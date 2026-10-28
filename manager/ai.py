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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import ClaudeConfig
from .db import EDITABLE, Event, Store

WEEKDAYS = "월화수목금토일"
OPS = ("add", "update", "done", "delete", "add_tag", "rename_tag")

INSTRUCTIONS = """너는 일정 관리 프로그램 'Manager'의 해석기야.
사용자의 한국어 요청을 읽고, 아래 JSON 객체 하나만 출력해. 설명·코드펜스·다른 텍스트 금지.

{"reply": "<사용자에게 보여줄 짧은 한국어 답변>",
 "actions": [<0개 이상의 액션>]}

액션 종류:
- {"op":"add","title":str,"start":str,"end":str|null,"all_day":bool,"deadline":bool,"category":str,"location":str,"notes":str}
- {"op":"update","id":int,"fields":{<바꿀 필드만: title,start,end,all_day,deadline,category,location,notes,status>}}
- {"op":"done","id":int}
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
- 수행평가·과제 제출·신청 마감처럼 "그날까지 끝내야 하는" 일정은 deadline=true (start에 마감일).
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
    return " ".join(parts)


def build_prompt(store: Store, message: str, history: list[tuple[str, str]] | None = None,
                 now: datetime | None = None, pending: "Plan | None" = None, feedback: str = "") -> str:
    now = now or datetime.now().astimezone()
    today = now.date()
    events = store.range((today - timedelta(days=14)).isoformat(), (today + timedelta(days=90)).isoformat())
    ev_text = "\n".join(_event_line(e) for e in events) or "(없음)"
    cats = ", ".join(store.categories()) or "(없음)"
    hist = ""
    if history:
        hist = "\n## 이전 대화\n" + "\n".join(f"사용자: {u}\nManager: {a}" for u, a in history)
    return (
        f"{INSTRUCTIONS}\n"
        f"## 현재 시각\n{now.isoformat(timespec='minutes')} ({WEEKDAYS[today.weekday()]}요일)\n\n"
        f"## 달력\n{_calendar_block(today)}\n\n"
        f"## 태그 목록 (category로 쓸 수 있는 값)\n{cats}\n\n"
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
def call_claude(prompt: str, cfg: ClaudeConfig) -> str:
    exe = shutil.which(cfg.command) or cfg.command
    cmd = [exe, "-p", "--output-format", "json"]
    if cfg.model:
        cmd += ["--model", cfg.model]
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
        return f"+ 추가: {a.get('title', '?')} | {when}{extra}"
    if op == "add_tag":
        return f"# 태그 추가: {a.get('name')}"
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
        return f"✓ 완료: {name}"
    return f"- 삭제: {name}"


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
                )
                ok.append(f"추가됨 #{ev.id} {ev.title}")
            elif op == "update":
                fields = {k: v for k, v in (a.get("fields") or {}).items() if k in EDITABLE}
                ev = store.update(int(a["id"]), **fields)
                ok.append(f"수정됨 #{ev.id} {ev.title}")
            elif op == "done":
                ev = store.update(int(a["id"]), status="done")
                ok.append(f"완료 #{ev.id} {ev.title}")
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
