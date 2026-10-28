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
from .db import Event, Store

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"  # database_id 기반 엔드포인트가 그대로 동작하는 안정 버전

PROPERTIES_SCHEMA = {
    "Name": {"title": {}},
    "Start Date": {"date": {}},
    "End Date": {"date": {}},
    "Done": {"checkbox": {}},  # 완료 = 체크
    "Category": {"select": {}},
    # 아래는 Notion에서만 관리하는 칸 — Manager는 값을 쓰지 않고 그대로 둔다
    "Type": {"multi_select": {}},
    "Team": {"multi_select": {}},
    "Priority": {"select": {}},
    "Teacher": {"multi_select": {}},
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
    # 마감형(deadline) 단일 날짜 일정은 End Date에만 넣는다 (예: 수행평가 마감)
    if e.deadline and not e.end:
        start, end = None, e.start
    else:
        start, end = e.start, e.end
    return {
        "Name": {"title": _text(e.title)},
        "Start Date": {"date": {"start": start} if start else None},
        "End Date": {"date": {"start": end} if end else None},
        "Done": {"checkbox": e.status == "done"},
        "Category": {"select": {"name": e.category[:100]} if e.category else None},
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
