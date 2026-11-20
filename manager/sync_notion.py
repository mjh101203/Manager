"""Notion 백업 — SQLite 변경분을 Notion 데이터베이스에 단방향 미러링.

- 각 일정은 Notion 페이지 1개. 페이지 id를 DB에 저장해서 이후엔 update.
- 로컬에서 삭제된 일정은 Notion 페이지를 archive(휴지통) 처리.
- 표준 라이브러리(urllib)만 사용.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import NotionConfig
from .db import Event, Store, now_iso

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"  # database_id 기반 엔드포인트가 그대로 동작하는 안정 버전

PROPERTIES_SCHEMA = {
    "Name": {"title": {}},
    "Start Date": {"date": {}},
    "End Date": {"date": {}},
    "Done": {"checkbox": {}},  # 완료 = 체크
    "Category": {"select": {}},
    "Teacher": {"multi_select": {}},
    "Submission Link": {"url": {}},
    "Team": {"multi_select": {}},
}


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str, min_interval: float = 0.35):
        if not token:
            raise NotionError("Notion 토큰 없음 (config.toml [notion].token 또는 NOTION_TOKEN)")
        self.token = token
        self.min_interval = min_interval  # 평균 3 req/s 제한 대응
        self._last = 0.0

    def request(self, method: str, path: str, body: dict | None = None, retries: int = 3) -> dict:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._last = time.monotonic()
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            self._last = time.monotonic()
            if e.code in (429, 502, 503) and retries > 0:
                time.sleep(float(e.headers.get("Retry-After", "1")))
                return self.request(method, path, body, retries - 1)
            detail = e.read().decode(errors="replace")
            raise NotionError(f"{method} {path} → HTTP {e.code}: {detail[:400]}") from None


def _text(s: str) -> list[dict]:
    s = s or ""
    return [{"type": "text", "text": {"content": s[:2000]}}] if s else []


def event_properties(e: Event) -> dict:
    # 마감형(deadline) 단일 날짜 일정: End Date = 마감, Start Date = 입력한 날짜
    if e.deadline and not e.end:
        start, end = (e.entered or None), e.start
    else:
        start, end = e.start, e.end
    return {
        "Name": {"title": _text(e.title)},
        "Start Date": {"date": {"start": start} if start else None},
        "End Date": {"date": {"start": end} if end else None},
        "Done": {"checkbox": e.status == "done"},
        "Category": {"select": {"name": e.category[:100]} if e.category else None},
        "Teacher": {"multi_select": [{"name": t[:100]} for t in e.teacher_list]},
        "Team": {"multi_select": [{"name": t[:100]} for t in e.team_list]},
        "Submission Link": {"url": e.submit_link[:2000] or None},  # 링크 또는 제출 방법 텍스트
    }


def create_database(client: NotionClient, parent_page_id: str, title: str = "Schedule Management") -> str:
    res = client.request("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": _text(title),
        "properties": PROPERTIES_SCHEMA,
    })
    return res["id"]


def ensure_category_options(store: Store, cfg: NotionConfig, client: NotionClient) -> list[str]:
    """로컬 태그 중 Notion Category 선택지에 없는 걸 추가. 추가한 이름 목록 반환.
    (일정이 하나도 없는 새 태그도 Notion에서 바로 고를 수 있게)"""
    db = client.request("GET", f"/databases/{cfg.database_id}")
    prop = db.get("properties", {}).get("Category", {})
    if prop.get("type") != "select":
        return []
    existing = prop["select"].get("options", [])
    names = {o["name"] for o in existing}
    missing = [t for t in store.categories() if t not in names]
    if missing:
        # 기존 옵션은 id로 그대로 넘겨야 값이 유지됨
        options = [{"id": o["id"]} for o in existing] + [{"name": t[:100]} for t in missing]
        client.request("PATCH", f"/databases/{cfg.database_id}",
                       {"properties": {"Category": {"select": {"options": options}}}})
    return missing


def _pages_with_category(client: NotionClient, cfg: NotionConfig, name: str) -> list[str]:
    """Category가 name인 페이지 id 전부 (Manager가 모르는 페이지 포함, 휴지통 제외)."""
    ids, cursor = [], None
    while True:
        body: dict = {"filter": {"property": "Category", "select": {"equals": name}}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = client.request("POST", f"/databases/{cfg.database_id}/query", body)
        ids += [p["id"] for p in res.get("results", [])]
        if not res.get("has_more"):
            return ids
        cursor = res.get("next_cursor")


def apply_tag_ops(store: Store, cfg: NotionConfig, client: NotionClient) -> tuple[int, list[str]]:
    """로컬에서 한 태그 이름 변경/삭제를 Notion DB 전체에 반영.
    old 태그가 붙은 모든 페이지를 new로 바꾸고(new == ''면 비움), old 선택지는 지운다.
    (바뀐 페이지 수, 오류 목록) 반환."""
    changed, errors = 0, []
    for op_id, old, new in store.pending_tag_ops():
        try:
            value = {"select": {"name": new[:100]}} if new else {"select": None}
            for page_id in _pages_with_category(client, cfg, old):
                client.request("PATCH", f"/pages/{page_id}", {"properties": {"Category": value}})
                changed += 1
            db = client.request("GET", f"/databases/{cfg.database_id}")
            existing = db.get("properties", {}).get("Category", {}).get("select", {}).get("options", [])
            if any(o["name"] == old for o in existing):
                keep = [{"id": o["id"]} for o in existing if o["name"] != old]
                if new and not any(o["name"] == new for o in existing):
                    keep.append({"name": new[:100]})
                client.request("PATCH", f"/databases/{cfg.database_id}",
                               {"properties": {"Category": {"select": {"options": keep}}}})
            store.mark_tag_op_done(op_id)
        except NotionError as err:
            errors.append(f"태그 '{old}' → '{new or '(없음)'}': {err}")
            break  # 순서가 중요하니 실패하면 다음 동기화 때 이어서
    return changed, errors


def sync(store: Store, cfg: NotionConfig, client: NotionClient | None = None) -> dict:
    if not cfg.database_id:
        raise NotionError("database_id 없음 — `manager notion-setup <부모페이지ID>`로 먼저 만들어줘")
    client = client or NotionClient(cfg.token)
    stats = {"created": 0, "updated": 0, "archived": 0, "errors": [], "new_tags": [], "retagged": 0}
    stats["retagged"], tag_errors = apply_tag_ops(store, cfg, client)
    stats["errors"] += tag_errors
    try:
        stats["new_tags"] = ensure_category_options(store, cfg, client)
    except NotionError as err:
        stats["errors"].append(f"태그 선택지 동기화: {err}")
    for e in store.notion_pending():
        try:
            if e.deleted:
                try:
                    client.request("PATCH", f"/pages/{e.notion_page_id}", {"archived": True})
                except NotionError as err:
                    if "404" not in str(err):  # 이미 지워진 페이지면 무시
                        raise
                store.mark_notion(e.id, None)
                stats["archived"] += 1
            elif e.notion_page_id:
                try:
                    client.request("PATCH", f"/pages/{e.notion_page_id}",
                                   {"properties": event_properties(e), "archived": False})
                    stats["updated"] += 1
                except NotionError as err:
                    if "404" not in str(err):
                        raise
                    # Notion에서 페이지가 영구 삭제됐으면 새로 생성
                    page = client.request("POST", "/pages", {
                        "parent": {"database_id": cfg.database_id}, "properties": event_properties(e)})
                    store.mark_notion(e.id, page["id"])
                    stats["created"] += 1
                    continue
                store.mark_notion(e.id, e.notion_page_id)
            else:
                page = client.request("POST", "/pages", {
                    "parent": {"database_id": cfg.database_id}, "properties": event_properties(e)})
                store.mark_notion(e.id, page["id"])
                stats["created"] += 1
        except NotionError as err:
            stats["errors"].append(f"#{e.id} {e.title}: {err}")
    return stats


# ---------------- Notion → 로컬 (pull) ----------------
def query_all(client: NotionClient, cfg: NotionConfig) -> list[dict]:
    """DB의 모든 페이지 (휴지통 제외). 실패하면 예외 — 일부만 받은 채로 삭제 판단하면 안 되니까."""
    pages, cursor = [], None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = client.request("POST", f"/databases/{cfg.database_id}/query", body)
        pages += res.get("results", [])
        if not res.get("has_more"):
            return pages
        cursor = res.get("next_cursor")


def _plain(rich: list[dict]) -> str:
    return "".join(x.get("plain_text") or x.get("text", {}).get("content", "") for x in rich or []).strip()


def _date_start(prop: dict) -> str | None:
    d = (prop or {}).get("date")
    return d.get("start") if d else None


def _to_dt(v: str):
    from datetime import datetime
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone()


def _same_time(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return not a and not b
    if len(a) == 10 or len(b) == 10:
        return a[:10] == b[:10] and len(a) == len(b)
    return _to_dt(a) == _to_dt(b)


def page_fields(page: dict, ev: Event | None = None) -> dict:
    """Notion 페이지 → 로컬 필드 값. 날짜가 하나도 없으면 start/end 키를 넣지 않음."""
    p = page.get("properties", {})
    out: dict = {
        "title": _plain(p.get("Name", {}).get("title")),
        "category": ((p.get("Category") or {}).get("select") or {}).get("name", ""),
        "teachers": ", ".join(o["name"] for o in (p.get("Teacher") or {}).get("multi_select", [])),
        "team": ", ".join(o["name"] for o in (p.get("Team") or {}).get("multi_select", [])),
        "submit_link": (p.get("Submission Link") or {}).get("url") or "",
        "done": bool((p.get("Done") or {}).get("checkbox")),
    }
    s, e = _date_start(p.get("Start Date")), _date_start(p.get("End Date"))
    if ev and ev.deadline and not ev.end and e:  # 마감 일정: Start = 입력일, End = 마감
        out.update(start=e, end=None, deadline=True)
        if s:
            out["entered"] = s[:10]
    elif not s and e:  # End만 있으면 마감 일정
        out.update(start=e, end=None, deadline=True)
    elif s:
        out.update(start=s, end=e)
        if ev and ev.deadline and not ev.end:  # 마감 일정을 Notion에서 Start로 옮겼으면 일반 일정
            out["deadline"] = False
    if "start" in out:
        out["all_day"] = len(out["start"]) == 10
    return out


def diff_fields(ev: Event, f: dict) -> dict:
    """Notion 값과 다른 로컬 필드만 (store.update에 넣을 형태)."""
    ch: dict = {}
    for k in ("title", "category", "teachers", "team", "submit_link", "entered"):
        if k in f and (f[k] or "") != (getattr(ev, k) or ""):
            if k == "title" and not f[k]:
                continue
            ch[k] = f[k]
    if "start" in f:
        if f["all_day"] != ev.all_day:
            ch["all_day"] = f["all_day"]
        if not _same_time(f["start"], ev.start) or "all_day" in ch:
            ch["start"] = f["start"]
        if not _same_time(f["end"], ev.end) or "all_day" in ch:
            ch["end"] = f["end"]
        if "deadline" in f and f["deadline"] != ev.deadline:
            ch["deadline"] = f["deadline"]
    if f["done"] and ev.status != "done":
        ch["status"] = "done"
    elif not f["done"] and ev.status == "done":
        ch["status"] = "todo"
    return ch


def _local_pending(store: Store, ev: Event) -> bool:
    r = store.conn.execute("SELECT updated_at, notion_synced_at FROM events WHERE id = ?", (ev.id,)).fetchone()
    return not r["notion_synced_at"] or r["updated_at"] > r["notion_synced_at"]


def _link(store: Store, event_id: int, page: dict) -> None:
    """페이지와 연결하고 동기화 완료로 표시. 로컬에서 값이 정리돼(제목 정리·선생님 자동 입력 등)
    Notion과 달라졌으면 미반영 상태로 남겨서 다음 push 때 Notion도 맞춘다."""
    store.mark_notion(event_id, page["id"])
    ev = store.get(event_id)
    if ev and diff_fields(ev, page_fields(page, ev)):
        store.conn.execute("UPDATE events SET updated_at = ? WHERE id = ?", (now_iso(), event_id))
        store.conn.commit()


def _norm_id(i: str) -> str:
    return (i or "").replace("-", "")


PULL_KINDS = ("update", "create", "delete", "conflict", "skip", "keep_local")


def plan_pull(store: Store, cfg: NotionConfig, client: NotionClient | None = None) -> tuple[list[dict], dict]:
    """Notion → 로컬 변경 계획. (items, pages) 반환. 아무것도 바꾸지 않음.
    item: {"no", "kind", "id", "title", "page_id", "changes"|"fields", "reason"}
      update   연결된 페이지 값이 바뀜 → 로컬에 반영
      conflict 로컬도 바뀜 → 기본은 로컬 유지 (kind를 update로 바꾸면 Notion 값 적용)
      keep_local  Notion 값 무시하고 로컬 값으로 Notion을 덮음
      create   DB에 새 페이지 → 로컬 일정 추가
      delete   DB에서 사라짐(휴지통·이동) → 로컬 삭제
      skip     가져올 수 없음 (날짜 없음 등)"""
    if not cfg.database_id:
        raise NotionError("database_id 없음")
    client = client or NotionClient(cfg.token)
    pages = query_all(client, cfg)
    by_id = {_norm_id(pg["id"]): pg for pg in pages}
    items: list[dict] = []
    linked: set[str] = set()
    for ev in store.all():
        if not ev.notion_page_id:
            continue
        pid = _norm_id(ev.notion_page_id)
        linked.add(pid)
        page = by_id.get(pid)
        if page is None:
            # 쿼리에 없음 → 정말 DB 밖으로 갔는지 페이지를 직접 확인
            try:
                info = client.request("GET", f"/pages/{ev.notion_page_id}")
                gone = info.get("archived") or info.get("in_trash") or \
                    _norm_id(info.get("parent", {}).get("database_id", "")) != _norm_id(cfg.database_id)
            except NotionError as err:
                gone = "404" in str(err)
                if not gone:
                    items.append({"kind": "skip", "id": ev.id, "title": ev.title, "page_id": ev.notion_page_id,
                                  "reason": f"확인 실패: {err}"})
                    continue
            if gone:
                items.append({"kind": "delete", "id": ev.id, "title": ev.title, "page_id": ev.notion_page_id})
            continue
        ch = diff_fields(ev, page_fields(page, ev))
        if ch:
            items.append({"kind": "conflict" if _local_pending(store, ev) else "update", "id": ev.id,
                          "title": ev.title, "page_id": page["id"], "changes": ch})
    for pid, page in by_id.items():
        if pid in linked:
            continue
        f = page_fields(page)
        if not f["title"]:
            continue
        item = {"kind": "create", "id": None, "title": f["title"], "page_id": page["id"], "fields": f}
        if "start" not in f:
            item.update(kind="skip", reason="날짜 없음 — 시작이나 마감 날짜를 정해주면 가져옴")
        items.append(item)
    for n, it in enumerate(items, 1):
        it["no"] = n
    return items, {_norm_id(pg["id"]): pg for pg in pages}


CREATE_KEYS = ("title", "start", "end", "all_day", "deadline", "category", "teachers", "team", "submit_link", "done")


def apply_pull(store: Store, items: list[dict], pages: dict) -> dict:
    """plan_pull(+수정) 결과를 로컬에 반영.
    반환: {"updated": [(id, title, changes)], "created": [(id, title)], "deleted": [(id, title)],
           "conflicts": [(id, title, changes)], "kept": [(id, title)], "skipped": [(id, title, reason)]}"""
    st: dict = {"updated": [], "created": [], "deleted": [], "conflicts": [], "kept": [], "skipped": []}
    for it in items:
        kind, eid, title = it.get("kind"), it.get("id"), it.get("title", "")
        page = pages.get(_norm_id(it.get("page_id", "")))
        try:
            if kind == "update":
                ev = store.get(eid) if eid else None
                if not ev:
                    raise KeyError(f"#{eid} 일정 없음")
                ch = {k: v for k, v in (it.get("changes") or {}).items()}
                if ch:
                    store.update(ev.id, **ch)
                if page:
                    _link(store, ev.id, page)
                st["updated"].append((ev.id, ev.title, ch))
            elif kind == "keep_local":
                # 동기화 시점보다 나중에 바뀐 걸로 표시 → push 때 로컬 값으로 Notion 덮음
                store.conn.execute("UPDATE events SET updated_at = ? WHERE id = ?", (now_iso(), eid))
                store.conn.commit()
                st["kept"].append((eid, title))
            elif kind == "conflict":
                st["conflicts"].append((eid, title, it.get("changes") or {}))
            elif kind == "delete":
                if store.get(eid):
                    store.delete(eid)
                    for s in store.sessions_of(eid):
                        store.delete(s.id)
                store.mark_notion(eid, None)  # 연결 끊기 → push 때 archive 안 함
                st["deleted"].append((eid, title))
            elif kind == "create":
                f = {k: v for k, v in (it.get("fields") or {}).items() if k in CREATE_KEYS}
                if not f.get("start"):
                    raise ValueError("날짜 없음")
                all_day = bool(f.get("all_day", len(str(f["start"])) == 10))
                ev = store.add(f.get("title") or title, f["start"], f.get("end"), all_day=all_day,
                               category=f.get("category") or "", deadline=bool(f.get("deadline")),
                               teachers=f.get("teachers"), team=f.get("team"))
                extra = {"submit_link": f["submit_link"]} if f.get("submit_link") else {}
                if f.get("done"):
                    extra["status"] = "done"
                if extra:
                    ev = store.update(ev.id, **extra)
                if page:
                    _link(store, ev.id, page)
                st["created"].append((ev.id, ev.title))
            else:
                st["skipped"].append((eid, title, it.get("reason", "")))
        except (ValueError, KeyError) as err:
            st["skipped"].append((eid, title, str(err)))
    return st


def pull(store: Store, cfg: NotionConfig, client: NotionClient | None = None, dry_run: bool = False) -> dict:
    """계획 + 반영 한 번에 (dry_run이면 계획만, apply_pull과 같은 형식으로)."""
    items, pages = plan_pull(store, cfg, client)
    if not dry_run:
        return apply_pull(store, items, pages)
    st: dict = {"updated": [], "created": [], "deleted": [], "conflicts": [], "kept": [], "skipped": []}
    for it in items:
        k = it["kind"]
        if k == "update":
            st["updated"].append((it["id"], it["title"], it["changes"]))
        elif k == "conflict":
            st["conflicts"].append((it["id"], it["title"], it["changes"]))
        elif k == "create":
            st["created"].append((None, it["title"]))
        elif k == "delete":
            st["deleted"].append((it["id"], it["title"]))
        else:
            st["skipped"].append((it["id"], it["title"], it.get("reason", "")))
    return st
