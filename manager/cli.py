"""Manager CLI 진입점."""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from . import ai, sync_notion, sync_obsidian
from .config import Config, load_config
from .db import Event, Store

WEEKDAYS = "월화수목금토일"


# ---------------- 출력 ----------------
def print_events(events: list[Event], empty: str = "(일정 없음)") -> None:
    if not events:
        print(empty)
        return
    cur = None
    for e in events:
        if e.day != cur:
            cur = e.day
            wd = WEEKDAYS[date.fromisoformat(cur).weekday()]
            mark = "  ← 오늘" if cur == date.today().isoformat() else ""
            print(f"\n■ {cur} ({wd}){mark}")
        box = {"done": "✓", "cancelled": "✗"}.get(e.status, "·")
        extra = "".join([f"  <{e.category}>" if e.category else "", f"  @{e.location}" if e.location else ""])
        print(f"  {box} #{e.id:<4} {e.time_label():<13} {e.title}{extra}")
        if e.notes:
            for ln in e.notes.splitlines():
                print(f"           {ln}")
    print()


YES = ("", "y", "yes", "ㅛ", "응", "ㅇ", "ㅇㅇ")
NO = ("n", "no", "ㅜ", "아니", "ㄴ", "ㄴㄴ")


def confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [Y/n] ").strip().lower()
    except EOFError:
        return False
    return ans in YES


def ask_decision() -> tuple[str, str]:
    """적용 확인. ('apply'|'revise'|'cancel', 수정요청)"""
    try:
        ans = input("\n적용할까? [Y=적용 / n=수정 / 바로 수정할 내용 입력] ").strip()
    except EOFError:
        return "cancel", ""
    if ans.lower() in YES:
        return "apply", ""
    if ans.lower() in NO:
        try:
            fb = input("어떻게 고칠까? (예: '2번 빼줘', '시간 8시로', 엔터=취소) ").strip()
        except EOFError:
            return "cancel", ""
        return ("revise", fb) if fb else ("cancel", "")
    return "revise", ans


# ---------------- 동기화 ----------------
def run_sync(store: Store, cfg: Config, notion: bool = True, obsidian: bool = True, quiet: bool = False) -> None:
    did = False
    if notion and cfg.notion.enabled:
        did = True
        try:
            s = sync_notion.sync(store, cfg.notion)
            msg = f"Notion: +{s['created']} ~{s['updated']} -{s['archived']}"
            if s.get("retagged"):
                msg += f"  (태그 변경으로 페이지 {s['retagged']}개 수정)"
            if s["errors"]:
                msg += f"  (오류 {len(s['errors'])}건)"
            if not quiet or s["errors"] or s["created"] or s["updated"] or s["archived"] or s.get("retagged"):
                print(msg)
            for err in s["errors"]:
                print("  !", err)
        except Exception as e:  # noqa: BLE001
            print(f"Notion 동기화 실패: {e}")
    if obsidian and cfg.obsidian.enabled:
        did = True
        try:
            s = sync_obsidian.sync(store, cfg.obsidian)
            if not quiet or s["written"] or s["removed"]:
                print(f"Obsidian: 작성 {s['written']} / 삭제 {s['removed']}")
        except Exception as e:  # noqa: BLE001
            print(f"Obsidian 동기화 실패: {e}")
    if not did and not quiet:
        print("켜진 백업 대상이 없음 — config.toml에서 notion/obsidian enabled = true")


def after_change(store: Store, cfg: Config) -> None:
    if cfg.auto_sync:
        run_sync(store, cfg, quiet=True)


# ---------------- AI 대화 ----------------
def handle_message(store: Store, cfg: Config, msg: str, history: list[tuple[str, str]],
                   assume_yes: bool = False) -> None:
    hist = history[-cfg.claude.history_turns:]
    plan: ai.Plan | None = None
    feedback = ""
    while True:
        print("… 생각 중")
        try:
            new = ai.interpret(store, msg, cfg.claude, hist, pending=plan, feedback=feedback)
        except ai.ClaudeError as e:
            print(f"[오류] {e}")
            return
        if feedback and not new.actions and plan and plan.actions:
            # 수정 요청에 되묻기만 왔으면 직전 제안은 유지
            print(f"\n{new.reply}")
        else:
            plan = new
            if plan.reply:
                print(f"\n{plan.reply}")
        if not plan.actions:
            history.append((msg, plan.reply))
            return
        print()
        for i, a in enumerate(plan.actions, 1):
            print(f"  {i}. " + ai.describe_action(store, a))
        if assume_yes or not cfg.confirm:
            decision, feedback = "apply", ""
        else:
            decision, feedback = ask_decision()
        if decision == "apply":
            ok, fail = ai.apply_actions(store, plan.actions)
            for m in ok:
                print("  ✔", m)
            for m in fail:
                print("  ✘", m)
            if ok:
                after_change(store, cfg)
            history.append((msg, plan.reply + f" [적용: {', '.join(ok) or '없음'}]"))
            return
        if decision == "cancel":
            print("취소함.")
            history.append((msg, plan.reply + " [사용자가 적용 취소]"))
            return
        # revise → 같은 요청 + 직전 제안 + 수정 요청으로 다시 해석


def chat(store: Store, cfg: Config) -> None:
    print("Manager — 자연어로 일정 관리. 명령: /list /today /tags /edit <번호> /sync /quit")
    history: list[tuple[str, str]] = []
    while True:
        try:
            msg = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        if msg in ("/q", "/quit", "/exit", "exit", "quit"):
            break
        if msg == "/list":
            print_events(store.range(date.today().isoformat(), (date.today() + timedelta(days=14)).isoformat()))
        elif msg == "/today":
            cmd_today(store)
        elif msg == "/sync":
            run_sync(store, cfg)
        elif msg == "/tags":
            print_tags(store)
        elif msg.startswith("/edit"):
            parts = msg.split(maxsplit=2)
            if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
                print("사용법: /edit <번호> [수정 내용]")
            else:
                cmd_edit(store, cfg, int(parts[1].lstrip("#")), parts[2] if len(parts) > 2 else "", {}, history)
        else:
            handle_message(store, cfg, msg, history)


# ---------------- 개별 명령 ----------------
def cmd_today(store: Store) -> None:
    t = date.today()
    ts = t.isoformat()
    past = store.range(None, ts, include_done=False)
    overdue = [e for e in past if (e.end or e.start)[:10] < ts]
    ongoing = [e for e in past if e not in overdue]
    if overdue:
        print("⚠ 지난 미완료")
        print_events(overdue)
    if ongoing:
        print("🔄 진행 중")
        print_events(ongoing)
    print_events([e for e in store.range(ts, (t + timedelta(days=1)).isoformat()) if e.day == ts],
                 "오늘 시작하는 일정 없음")


def print_tags(store: Store) -> None:
    tags = store.tags()
    if not tags:
        print("(태그 없음)")
        return
    for name, n in tags:
        print(f"  {name:<20} {n}건")


def cmd_tag(store: Store, cfg: Config, action: str, names: list[str], move_to: str) -> None:
    if action == "list":
        print_tags(store)
        return
    if action == "add":
        for n in names:
            print(("추가됨: " if store.add_tag(n) else "이미 있음: ") + n)
    elif action == "rename":
        if len(names) != 2:
            raise ValueError("사용법: manager tag rename <기존> <새이름>")
        cnt = store.rename_tag(names[0], names[1])
        print(f"{names[0]} → {names[1]} (일정 {cnt}건 변경)")
    elif action == "rm":
        for n in names:
            if move_to and n == move_to:
                raise ValueError("같은 태그로는 옮길 수 없음")
            cnt = store.remove_tag(n, move_to)
            dest = f"'{move_to}'(으)로 이동" if move_to else "태그 없음으로 변경"
            print(f"삭제됨: {n} (일정 {cnt}건 {dest})")
    after_change(store, cfg)


EDIT_FIELDS = [  # (필드, 표시 이름)
    ("title", "제목"), ("start", "시작"), ("end", "종료 ('-' = 비우기)"), ("category", "태그"),
    ("location", "장소"), ("notes", "메모"), ("deadline", "마감 일정 (y/n)"), ("status", "상태 todo/done/cancelled"),
]


def _ask(label: str, current: str) -> str | None:
    """엔터면 None(유지)."""
    try:
        v = input(f"  {label} [{current}]: ").strip()
    except EOFError:
        return None
    return v or None


def interactive_edit(store: Store, ev: Event) -> dict:
    print(f"#{ev.id} 수정 — 엔터를 누르면 그대로 유지")
    if store.categories():
        print("  태그 목록: " + ", ".join(store.categories()))
    fields: dict = {}
    for key, label in EDIT_FIELDS:
        cur = getattr(ev, key)
        cur_s = ("y" if cur else "n") if key == "deadline" else (cur or "")
        v = _ask(label, cur_s)
        if v is None:
            continue
        if key == "deadline":
            fields[key] = v.lower() in YES + ("true",)
        elif key == "end" and v == "-":
            fields[key] = None
        else:
            fields[key] = v
    return fields


def cmd_edit(store: Store, cfg: Config, event_id: int, text: str, flag_fields: dict,
             history: list[tuple[str, str]] | None = None) -> None:
    ev = store.get(event_id)
    if not ev:
        raise KeyError(f"#{event_id} 일정 없음")
    print_events([ev])
    if text:  # 자연어 → AI (이 일정만 대상으로)
        msg = f"#{ev.id} 일정({ai._event_line(ev)})을 이렇게 수정해줘: {text}"
        handle_message(store, cfg, msg, history if history is not None else [])
        return
    fields = flag_fields or interactive_edit(store, ev)
    if not fields:
        print("바뀐 거 없음.")
        return
    cat = fields.get("category")
    if cat and not store.has_tag(cat) and not confirm(f"'{cat}'은(는) 새 태그야. 만들까?"):
        print("취소함.")
        return
    ev = store.update(event_id, **fields)
    print(f"수정됨 #{ev.id} {ev.title} ({ev.day} {ev.time_label()})" + (f" <{ev.category}>" if ev.category else ""))
    after_change(store, cfg)


def init_config(target: Path) -> None:
    src = Path(__file__).resolve().parent.parent / "config.example.toml"
    if target.exists():
        print(f"이미 있음: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copy(src, target)
    else:
        target.write_text('db_path = "manager.db"\n', encoding="utf-8")
    print(f"설정 파일 생성: {target}\n열어서 notion/obsidian 항목 채워줘.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="manager", description="AI 일정 관리 + Notion/Obsidian 백업")
    p.add_argument("--config", help="config.toml 경로")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("chat", help="대화 모드 (기본)")
    s = sub.add_parser("ask", help="한 번만 자연어 요청")
    s.add_argument("text", nargs="+")
    s.add_argument("-y", "--yes", action="store_true", help="확인 없이 적용")

    s = sub.add_parser("add", help="직접 추가 (AI 없이)")
    s.add_argument("title")
    s.add_argument("start", help='"2026-10-28 15:00" 또는 종일이면 "2026-10-28"')
    s.add_argument("--end")
    s.add_argument("--all-day", action="store_true")
    s.add_argument("--deadline", action="store_true", help="마감형 일정 (Notion End Date에 기록)")
    s.add_argument("-c", "--category", default="")
    s.add_argument("-l", "--location", default="")
    s.add_argument("-n", "--notes", default="")

    s = sub.add_parser("list", help="목록")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--from", dest="from_", help="YYYY-MM-DD (기본 오늘)")
    s.add_argument("--all", action="store_true", help="전체")
    s.add_argument("--todo", action="store_true", help="미완료만")

    sub.add_parser("today", help="오늘 + 지난 미완료")

    s = sub.add_parser("done", help="완료 처리")
    s.add_argument("ids", type=int, nargs="+")
    s = sub.add_parser("rm", help="삭제")
    s.add_argument("ids", type=int, nargs="+")

    s = sub.add_parser("edit", help="일정 수정 (번호만 주면 대화형, 문장을 주면 AI가 수정)")
    s.add_argument("id", type=int)
    s.add_argument("text", nargs="*", help='자연어 수정 요청, 예: "30분 늦춰줘"')
    for f in ("title", "start", "end", "location", "notes", "status"):
        s.add_argument(f"--{f}")
    s.add_argument("-c", "--category", "--tag", dest="category")
    s.add_argument("--deadline", dest="deadline", action="store_true", default=None, help="마감 일정으로")
    s.add_argument("--no-deadline", dest="deadline", action="store_false", help="마감 일정 해제")

    s = sub.add_parser("tag", help="태그(카테고리) 관리")
    s.add_argument("action", nargs="?", default="list", choices=["list", "add", "rename", "rm"])
    s.add_argument("names", nargs="*")
    s.add_argument("--to", dest="move_to", default="", help="rm 할 때 일정을 옮길 태그")

    s = sub.add_parser("sync", help="Notion/Obsidian 백업")
    s.add_argument("--notion", action="store_true", help="Notion만")
    s.add_argument("--obsidian", action="store_true", help="Obsidian만")

    s = sub.add_parser("notion-setup", help="Notion에 일정 DB 생성")
    s.add_argument("parent_page_id", help="DB를 만들 부모 페이지 ID (integration이 공유돼 있어야 함)")

    s = sub.add_parser("init", help="설정 파일 템플릿 생성")
    s.add_argument("--path", default=str(Path.home() / ".manager" / "config.toml"))
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)

    if args.cmd == "init":
        init_config(Path(args.path))
        return 0

    cfg = load_config(args.config)
    store = Store(cfg.db_path)
    cmd = args.cmd or "chat"

    try:
        if cmd == "chat":
            chat(store, cfg)
        elif cmd == "ask":
            handle_message(store, cfg, " ".join(args.text), [], assume_yes=args.yes)
        elif cmd == "add":
            ev = store.add(args.title, args.start, args.end, args.all_day, args.category, args.location, args.notes,
                           deadline=args.deadline)
            print(f"추가됨 #{ev.id} {ev.title} ({ev.day} {ev.time_label()})")
            after_change(store, cfg)
        elif cmd == "list":
            if args.all:
                evs = store.all()
                evs = [e for e in evs if e.status == "todo"] if args.todo else evs
            else:
                start = date.fromisoformat(args.from_) if args.from_ else date.today()
                evs = store.range(start.isoformat(), (start + timedelta(days=args.days)).isoformat(),
                                  include_done=not args.todo)
            print_events(evs)
        elif cmd == "today":
            cmd_today(store)
        elif cmd == "done":
            for i in args.ids:
                print("완료", store.update(i, status="done").title)
            after_change(store, cfg)
        elif cmd == "rm":
            for i in args.ids:
                print("삭제", store.delete(i).title)
            after_change(store, cfg)
        elif cmd == "edit":
            fields = {k: v for k in ("title", "start", "end", "category", "location", "notes", "status", "deadline")
                      if (v := getattr(args, k)) is not None}
            if fields.get("end") == "-":
                fields["end"] = None
            cmd_edit(store, cfg, args.id, " ".join(args.text), fields)
        elif cmd == "tag":
            cmd_tag(store, cfg, args.action, args.names, args.move_to)
        elif cmd == "sync":
            both = not (args.notion or args.obsidian)
            run_sync(store, cfg, notion=both or args.notion, obsidian=both or args.obsidian)
        elif cmd == "notion-setup":
            client = sync_notion.NotionClient(cfg.notion.token)
            db_id = sync_notion.create_database(client, args.parent_page_id)
            print(f"Notion DB 생성 완료.\nconfig.toml [notion] 에 넣어줘:\n  database_id = \"{db_id}\"")
    except (KeyError, ValueError) as e:
        print(f"[오류] {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
