"""Claude Code CLI(`claude -p`)를 '두뇌'로 쓰는 부분.

AI는 DB를 직접 건드리지 않는다. 자연어를 아래 JSON '계획'으로 바꾸기만 하고,
실제 적용은 Python(apply_actions)이 검증 후 수행한다.

{
  "reply": "사용자에게 보여줄 한국어 답변",
  "actions": [
    {"op": "add", "title": "...", "start": "...", "end": null, "all_day": false,
     "category": "", "location": "", "notes": ""},
    {"op": "update", "id": 3, "fields": {"start": "..."}},
    {"op": "done", "id": 3},
    {"op": "delete", "id": 3}
  ]
}
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import ClaudeConfig
from .db import AUTO_TEACHER_CATEGORY, EDITABLE, Event, Store, split_names

WEEKDAYS = "월화수목금토일"
OPS = ("add", "update", "done", "delete", "add_tag", "rename_tag", "plan")

INSTRUCTIONS = """너는 일정 관리 프로그램 'Manager'의 해석기야.
사용자의 한국어 요청을 읽고, 아래 JSON 객체 하나만 출력해. 설명·코드펜스·다른 텍스트 금지.

{"reply": "<사용자에게 보여줄 짧은 한국어 답변>",
 "actions": [<0개 이상의 액션>]}

액션 종류:
- {"op":"add","title":str,"start":str,"end":str|null,"all_day":bool,"deadline":bool,"category":str,"subject":str,"teachers":[str],"team":[str],"topic":str,"scope":str,"submit_link":str,"location":str,"notes":str}
- {"op":"update","id":int,"fields":{<바꿀 필드만: title,start,end,all_day,deadline,category,subject,teachers,team,topic,scope,submit_link,location,notes,status>}}
- {"op":"done","id":int,"actual_minutes":int|null}   # 실제 걸린 시간을 말했으면 분으로
- {"op":"plan","id":int|null,"title":str,"note":str}  # 과제 수행 일정(작업 세션) 짜기. 같은 요청에서 새로 추가한 과제면 id 대신 title. note엔 "주말 위주" 같은 요청
- {"op":"delete","id":int}
- {"op":"add_tag","name":str}                 # 새 태그(카테고리) 만들기
- {"op":"rename_tag","old":str,"new":str}     # 태그 이름 변경 (그 태그의 일정도 같이 바뀜)

규칙:
- 시간 일정 start/end는 "YYYY-MM-DDTHH:MM" (현지 시간), 종일 일정은 "YYYY-MM-DD"에 all_day=true.
- 상대 날짜(내일, 다음주 화요일 등)는 아래 '현재 시각'과 '달력'으로 정확히 계산해.
- 시간만 있고 종료가 없으면 end는 null. "~부터 ~까지"면 end를 채워.
- 날짜가 아예 없거나 모호해서 확정할 수 없으면 actions를 비우고 reply로 되물어.
- 기존 일정을 수정/완료/삭제할 땐 반드시 아래 목록의 id를 써. 대상이 여러 개로 모호하면 되묻기.
- category는 아래 '태그 목록'에 있는 이름을 그대로 써. 맞는 게 없으면 ""로 두고 reply에서 새 태그를 제안해.
- 사용자가 새 태그를 만들거나 쓰라고 하면 add_tag 액션을 먼저 넣고 그 이름을 category에 써.
- 조회 질문(오늘 뭐 있어? 등)은 actions를 비우고 reply에 목록 기반으로 답해.
- status 값: todo | done | cancelled
- title은 할 일 이름만 (예: "캔위성 계획서"). "제출", "마감", "~까지", 날짜 같은 말은 title에 넣지 마.
  마감은 deadline=true, 제출 링크(없으면 제출 방법)는 submit_link에 넣어.
- team(팀원)은 사용자가 말한 팀원 이름 목록. '팀원 목록'에 있는 표기를 그대로 쓰고, 혼자면 [] (notes에 팀원을 쓰지 마).
- teachers(담당 선생님)는 사용자가 말했을 때만 넣어. 이름은 '선생님 목록'에 있는 표기를 그대로 써 (예: "최한욱 선생님").
- submit_link 하나만 써: 제출 링크(URL)가 있으면 링크만, 링크가 없으면 제출 방법을 텍스트로 (예: "드롭박스", "교무실 앞 제출함"). 말 안 했으면 "".
- 과제 주제(예: "해밍 코드")는 topic에, 분량·형식·산출물 같은 요구사항은 scope에 넣어. 말 안 했으면 "".
- Assignment 일정이면 subject에 '과목별 선생님' 목록 중 해당 과목을 넣어 (선생님은 프로그램이 자동으로 채움). 맞는 과목이 없으면 "".
- 수행평가·과제 제출·신청 마감처럼 "그날까지 끝내야 하는" 일정은 deadline=true.
  시작일을 말했으면 start=시작일, end=마감일. 시작일을 말 안 했으면 start=마감일, end=null (시작은 프로그램이 입력한 날짜로 채움).
- "일정 짜줘/계획 세워줘/언제 하면 돼?"처럼 과제를 언제 할지 묻는 요청은 plan 액션 (시간 배치는 프로그램이 함).
- reply는 반말로 짧게. 액션은 사용자 확인 후 적용되니 "~추가할게", "~옮길게"처럼 제안형으로 써.
"""


@dataclass
class Plan:
    reply: str
    actions: list[dict] = field(default_factory=list)


class ClaudeError(RuntimeError):
    pass


# ---------------- 프롬프트 ----------------
def _calendar_block(today: date, days: int = 21) -> str:
    lines = []
    for i in range(days):
        d = today + timedelta(days=i)
        tag = " (오늘)" if i == 0 else " (내일)" if i == 1 else ""
        lines.append(f"{d.isoformat()} {WEEKDAYS[d.weekday()]}{tag}")
    return "\n".join(lines)


def _event_line(e: Event) -> str:
    wd = WEEKDAYS[date.fromisoformat(e.day).weekday()]
    parts = [f"#{e.id}", f"[{e.status}]", f"{e.day}({wd})", e.time_label(), e.title]
    if e.category:
        parts.append(f"<{e.category}>")
    if e.location:
        parts.append(f"@{e.location}")
    if e.teachers:
        parts.append(f"[{e.teachers}]")
    if e.team:
        parts.append(f"(팀: {e.team})")
    return " ".join(parts)


def build_prompt(store: Store, message: str, history: list[tuple[str, str]] | None = None,
                 now: datetime | None = None, pending: "Plan | None" = None, feedback: str = "") -> str:
    now = now or datetime.now().astimezone()
    today = now.date()
    events = store.range((today - timedelta(days=14)).isoformat(), (today + timedelta(days=90)).isoformat())
    ev_text = "\n".join(_event_line(e) for e in events) or "(없음)"
    cats = ", ".join(store.categories()) or "(없음)"
    subj = ", ".join(f"{s}={t}" for s, t in store.subject_teachers()) or "(없음)"
    teachers = ", ".join(store.known_teachers()) or "(없음)"
    members = ", ".join(store.known_members()) or "(없음)"
    hist = ""
    if history:
        hist = "\n## 이전 대화\n" + "\n".join(f"사용자: {u}\nManager: {a}" for u, a in history)
    return (
        f"{INSTRUCTIONS}\n"
        f"## 현재 시각\n{now.isoformat(timespec='minutes')} ({WEEKDAYS[today.weekday()]}요일)\n\n"
        f"## 달력\n{_calendar_block(today)}\n\n"
        f"## 태그 목록 (category로 쓸 수 있는 값)\n{cats}\n\n"
        f"## 과목별 선생님 (과목=선생님)\n{subj}\n\n"
        f"## 선생님 목록\n{teachers}\n\n"
        f"## 팀원 목록\n{members}\n\n"
        f"## 일정 목록 (최근 2주 ~ 향후 90일)\n{ev_text}\n"
        f"{hist}\n\n## 사용자 요청\n{message}\n"
        f"{_revision_block(store, pending, feedback)}"
    )


def _revision_block(store: Store, pending: "Plan | None", feedback: str) -> str:
    if not pending or not feedback:
        return ""
    numbered = "\n".join(f"{i}. {describe_action(store, a)}" for i, a in enumerate(pending.actions, 1))
    return (
        "\n## 직전 제안 (아직 적용 안 됨)\n"
        f"{numbered}\n{json.dumps(pending.actions, ensure_ascii=False)}\n\n"
        f"## 수정 요청\n{feedback}\n"
        "→ 직전 제안을 수정 요청대로 고친 '최종 actions 전체'를 다시 출력해. "
        "언급 안 된 항목은 그대로 유지하고, 번호는 위 목록 기준이야.\n"
    )


# ---------------- Claude 호출 ----------------
def call_claude(prompt: str, cfg: ClaudeConfig, read_files: list[str] | None = None) -> str:
    """read_files: Claude가 Read 도구로 직접 읽어도 되는 파일 (과제 안내문 등)."""
    exe = shutil.which(cfg.command) or cfg.command
    cmd = [exe, "-p", "--output-format", "json"]
    if cfg.model:
        cmd += ["--model", cfg.model]
    if read_files:
        dirs = sorted({str(Path(f).expanduser().resolve().parent) for f in read_files})
        cmd += ["--allowedTools", "Read"]
        for d in dirs:
            cmd += ["--add-dir", d]
    cmd += list(cfg.extra_args)
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=cfg.timeout)
    except FileNotFoundError:
        raise ClaudeError(f"'{cfg.command}' 실행 파일을 못 찾음. Claude Code가 설치돼 있고 PATH에 있는지 확인.")
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"claude 응답 시간 초과 ({cfg.timeout}s)")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        raise ClaudeError(f"claude 실패 (exit {proc.returncode}): {(proc.stderr or '').strip()[:500]}")
    # --output-format json → {"type":"result","result":"...","is_error":false,...}
    try:
        wrapper = json.loads(out)
    except json.JSONDecodeError:
        return out  # 구버전/텍스트 출력이면 그대로
    if isinstance(wrapper, dict) and "result" in wrapper:
        if wrapper.get("is_error"):
            raise ClaudeError(f"claude 오류: {wrapper.get('result')}")
        return str(wrapper["result"])
    return out


def extract_json(text: str) -> dict:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j <= i:
            raise ValueError("응답에서 JSON을 못 찾음")
        t = t[i:j + 1]
    return json.loads(t)


def parse_plan(text: str) -> Plan:
    try:
        data = extract_json(text)
    except (ValueError, json.JSONDecodeError):
        # JSON이 아니면 그냥 답변으로 취급 (액션 없음)
        return Plan(reply=text.strip(), actions=[])
    actions = [a for a in data.get("actions", []) if isinstance(a, dict) and a.get("op") in OPS]
    return Plan(reply=str(data.get("reply", "")).strip(), actions=actions)


def interpret(store: Store, message: str, cfg: ClaudeConfig,
              history: list[tuple[str, str]] | None = None,
              pending: Plan | None = None, feedback: str = "") -> Plan:
    prompt = build_prompt(store, message, history, pending=pending, feedback=feedback)
    return parse_plan(call_claude(prompt, cfg))


# ---------------- 미리보기 & 적용 ----------------
def describe_action(store: Store, a: dict) -> str:
    op = a["op"]
    if op == "add":
        when = a.get("start", "?") + (f" ~ {a['end']}" if a.get("end") else "")
        if a.get("all_day"):
            when += " (종일)"
        if a.get("deadline"):
            when += " (마감)"
        cat = a.get("category") or ""
        extra = f" <{cat}>" if cat else ""
        if cat and not store.has_tag(cat):
            extra += " (새 태그)"
        t_list = split_names(a.get("teachers"))
        if not t_list and cat == AUTO_TEACHER_CATEGORY:
            _, t_list = store._auto_teacher(a.get("title", ""), a.get("subject", ""))
            if t_list:
                extra += f" 👤{', '.join(t_list)} (자동)"
                t_list = []
        if t_list:
            extra += f" 👤{', '.join(t_list)}"
        if split_names(a.get("team")):
            extra += f" 👥{', '.join(split_names(a.get('team')))}"
        if a.get("submit_link") or a.get("submit_via"):
            extra += f" 📤{a.get('submit_link') or a.get('submit_via')}"
        return f"+ 추가: {a.get('title', '?')} | {when}{extra}"
    if op == "add_tag":
        return f"# 태그 추가: {a.get('name')}"
    if op == "plan":
        who = f"#{a['id']}" if a.get("id") else a.get("title", "?")
        return f"🗓 수행 일정 짜기: {who}" + (f" — {a['note']}" if a.get("note") else "")
    if op == "rename_tag":
        return f"# 태그 이름 변경: {a.get('old')} → {a.get('new')}"
    ev = store.get(int(a.get("id", -1)))
    name = f"#{a.get('id')} {ev.title}" if ev else f"#{a.get('id')} (없는 일정!)"
    if op == "update":
        fields = ", ".join(f"{k}={v!r}" for k, v in (a.get("fields") or {}).items())
        new_cat = (a.get("fields") or {}).get("category")
        if new_cat and not store.has_tag(new_cat):
            fields += " (새 태그)"
        return f"~ 수정: {name} → {fields}"
    if op == "done":
        took = f" (실제 {a['actual_minutes']}분)" if a.get("actual_minutes") else ""
        return f"✓ 완료: {name}{took}"
    return f"- 삭제: {name}"


def mark_done(store: Store, event_id: int, actual_minutes=None) -> Event:
    """완료 처리. 과제면 실제 소요시간 기록 + 남은 예정 세션 정리."""
    ev = store.update(event_id, status="done")
    fields = {}
    if actual_minutes:
        fields["actual_minutes"] = int(actual_minutes)
    elif ev.est_minutes and not ev.parent_id and not ev.actual_minutes:
        done = sum(s.est_minutes or 0 for s in store.sessions_of(ev.id) if s.status == "done")
        if done:
            fields["actual_minutes"] = done
    if fields:
        ev = store.update(event_id, **fields)
    if not ev.parent_id:
        for s in store.sessions_of(ev.id):
            if s.status == "todo":
                store.delete(s.id)
    return ev


def resolve_plan_targets(store: Store, actions: list[dict]) -> list[tuple[int, str]]:
    """plan 액션 → (과제 id, note). title만 있으면 제목으로 찾기 (방금 추가한 과제)."""
    out = []
    for a in actions:
        if a.get("op") != "plan":
            continue
        eid = a.get("id")
        if not eid and a.get("title"):
            hits = [e for e in store.all() if e.title == a["title"] and not e.parent_id]
            eid = hits[-1].id if hits else None
        if eid:
            out.append((int(eid), a.get("note") or ""))
    return out


def apply_actions(store: Store, actions: list[dict]) -> tuple[list[str], list[str]]:
    """(성공 메시지, 실패 메시지) 반환. 하나 실패해도 나머지는 진행."""
    ok, fail = [], []
    # 태그 액션을 먼저 적용해야 같은 요청 안의 일정이 그 태그를 쓸 수 있음
    actions = sorted(actions, key=lambda a: 0 if a.get("op") in ("add_tag", "rename_tag") else 1)
    for a in actions:
        try:
            op = a["op"]
            if op == "add":
                ev = store.add(
                    title=a["title"], start=a["start"], end=a.get("end"), all_day=bool(a.get("all_day")), deadline=bool(a.get("deadline")),
                    category=a.get("category") or "", location=a.get("location") or "", notes=a.get("notes") or "",
                    teachers=a.get("teachers"), subject=a.get("subject") or "", team=a.get("team"),
                )
                extra = {k: a[k] for k in ("topic", "scope", "submit_link") if a.get(k)}
                if not a.get("submit_link") and a.get("submit_via"):  # 옛 형식 대비
                    extra["submit_link"] = a["submit_via"]
                if extra:
                    ev = store.update(ev.id, **extra)
                ok.append(f"추가됨 #{ev.id} {ev.title}")
            elif op == "update":
                fields = {k: v for k, v in (a.get("fields") or {}).items() if k in EDITABLE}
                ev = store.update(int(a["id"]), **fields)
                ok.append(f"수정됨 #{ev.id} {ev.title}")
            elif op == "done":
                ev = mark_done(store, int(a["id"]), a.get("actual_minutes"))
                ok.append(f"완료 #{ev.id} {ev.title}")
            elif op == "plan":
                continue  # 계획은 cli가 따로 진행 (미리보기·확인 필요)
            elif op == "delete":
                ev = store.delete(int(a["id"]))
                ok.append(f"삭제됨 #{ev.id} {ev.title}")
            elif op == "add_tag":
                created = store.add_tag(str(a["name"]))
                ok.append(f"태그 {'추가됨' if created else '이미 있음'}: {a['name']}")
            elif op == "rename_tag":
                n = store.rename_tag(str(a["old"]), str(a["new"]))
                ok.append(f"태그 변경: {a['old']} → {a['new']} (일정 {n}건)")
        except Exception as e:  # noqa: BLE001
            fail.append(f"{a.get('op')} 실패: {e}")
    return ok, fail


# ---------------- Notion 가져오기(pull) 미리보기 수정 ----------------
PULL_REVISE_INSTRUCTIONS = """너는 일정 관리 프로그램의 'Notion → 로컬 가져오기' 미리보기를 사용자 요청대로 고치는 역할이야.
아래 items는 적용 예정 목록이야. 사용자 요청을 반영한 items 전체를 JSON으로만 답해:
{"reply": "무엇을 바꿨는지 한두 문장 (한국어)", "items": [...]}

규칙:
- 각 item의 no, page_id, id는 그대로 둬. 새 item을 만들지 마. 빼달라는 항목은 목록에서 지워 (적용 안 함).
- kind 의미:
  update = Notion 값(changes)을 로컬에 반영 / conflict = 양쪽 다 바뀜, 적용 안 하고 로컬 유지
  keep_local = Notion 값을 무시하고 로컬 값으로 Notion을 덮음
  create = Notion의 새 페이지를 로컬 일정으로 추가 / delete = 로컬에서 삭제 / skip = 아무것도 안 함
- 바꿀 수 있는 kind: update·conflict ↔ update/conflict/keep_local, create ↔ create/skip(날짜 있을 때만 create), delete ↔ delete/skip.
- changes / fields 값은 사용자가 고치라는 대로 수정 가능. 날짜는 "YYYY-MM-DD" 또는 "YYYY-MM-DDTHH:MM+09:00".
  마감만 있는 일정이면 start에 마감일, end는 null, deadline=true. status는 todo|done|cancelled.
- 날짜 없어서 skip된 항목에 사용자가 날짜를 주면 fields.start(필요하면 deadline)를 채우고 kind를 create로.
- 요청이 애매하면 items는 그대로 두고 reply에 되물어.
"""

PULL_CHANGE_KEYS = ("title", "category", "teachers", "team", "submit_link", "entered", "start", "end", "all_day", "deadline", "status")
PULL_FIELD_KEYS = ("title", "start", "end", "all_day", "deadline", "category", "teachers", "team", "submit_link", "done")
_PULL_ALLOWED = {"update": {"update", "conflict", "keep_local"}, "conflict": {"update", "conflict", "keep_local"},
                 "create": {"create", "skip"}, "delete": {"delete", "skip"}}


def sanitize_pull_items(new_items: list, original: list[dict]) -> list[dict]:
    """AI가 고친 목록을 원래 목록 기준으로 검증. 없는 항목·허용 안 되는 변경은 버림."""
    orig = {it["no"]: it for it in original}
    out = []
    for raw in new_items or []:
        if not isinstance(raw, dict) or raw.get("no") not in orig:
            continue
        o = orig[raw["no"]]
        base = "create" if (o["kind"] == "skip" and o.get("fields")) else o["kind"]
        kind = raw.get("kind", o["kind"])
        if kind != o["kind"] and kind not in _PULL_ALLOWED.get(base, set()):
            kind = o["kind"]
        it = {k: o.get(k) for k in ("no", "id", "page_id", "title")}
        it["kind"] = kind
        if "changes" in o:
            ch = raw.get("changes") if isinstance(raw.get("changes"), dict) else o["changes"]
            it["changes"] = {k: v for k, v in ch.items() if k in PULL_CHANGE_KEYS}
        if "fields" in o:
            f = raw.get("fields") if isinstance(raw.get("fields"), dict) else o["fields"]
            it["fields"] = {k: v for k, v in f.items() if k in PULL_FIELD_KEYS}
            it["title"] = it["fields"].get("title") or o["title"]
            if kind == "create" and not it["fields"].get("start"):
                it["kind"] = "skip"
        if it["kind"] == "skip":
            it["reason"] = raw.get("reason") or o.get("reason") or "사용자가 제외"
        out.append(it)
    return out


def revise_pull(items: list[dict], request: str, cfg: ClaudeConfig) -> tuple[str, list[dict]]:
    today = date.today()
    prompt = (PULL_REVISE_INSTRUCTIONS
              + f"\n오늘: {today.isoformat()} ({'월화수목금토일'[today.weekday()]})\n"
              + "items:\n" + json.dumps(items, ensure_ascii=False, indent=1)
              + f"\n\n사용자 요청: {request}\n")
    data = extract_json(call_claude(prompt, cfg))
    return str(data.get("reply", "")), sanitize_pull_items(data.get("items"), items)
