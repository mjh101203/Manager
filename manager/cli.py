"""Manager CLI 진입점."""
from __future__ import annotations

import argparse
import shutil
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from . import ai, planner, school, sync_notion, sync_obsidian
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
        extra = "".join([f"  <{e.category}>" if e.category else "", f"  @{e.location}" if e.location else "",
                         f"  👤{e.teachers}" if e.teachers else "", f"  👥{e.team}" if e.team else ""])
        if e.parent_id:
            extra += f"  ⏱{e.est_minutes or '?'}분 (#{e.parent_id} 작업)"
        elif e.est_minutes:
            extra += f"  ⏱예상 {e.est_minutes}분" + (f" / 실제 {e.actual_minutes}분" if e.actual_minutes else "")
        if e.topic:
            extra += f"  📝{e.topic}"
        if e.submit_link:
            extra += f"  📤{e.submit_link}"
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
        ans = input("\n적용할까? [Y=적용 / n=취소 / 고칠 내용 입력 (예: 2번 빼줘, 시간 8시로)] ").strip()
    except EOFError:
        return "cancel", ""
    if ans.lower() in YES:
        return "apply", ""
    if ans.lower() in NO:
        return "cancel", ""
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


FIELD_NAMES = {"title": "제목", "category": "태그", "teachers": "선생님", "team": "팀원", "submit_link": "제출",
               "start": "시작", "end": "종료", "entered": "입력일", "all_day": "종일", "deadline": "마감", "status": "상태"}


def _fmt_changes(ch: dict) -> str:
    return ", ".join(f"{FIELD_NAMES.get(k, k)}→{v if v not in (None, '') else '(비움)'}" for k, v in ch.items())


def _fmt_fields(f: dict) -> str:
    parts = []
    if f.get("start"):
        parts.append(f["start"] + (f" ~ {f['end']}" if f.get("end") else "") + (" (마감)" if f.get("deadline") else ""))
    if f.get("category"):
        parts.append(f"<{f['category']}>")
    if f.get("teachers"):
        parts.append(f"👤{f['teachers']}")
    if f.get("team"):
        parts.append(f"👥{f['team']}")
    if f.get("submit_link"):
        parts.append(f"📤{f['submit_link']}")
    if f.get("done"):
        parts.append("✓완료")
    return " ".join(parts)


PULL_LABEL = {"update": "~ 수정", "create": "+ 추가", "delete": "- 삭제", "conflict": "! 충돌(로컬 유지)",
              "keep_local": "↺ 로컬 값으로 Notion 덮기", "skip": "· 건너뜀"}


def print_pull_preview(items: list[dict]) -> None:
    if not items:
        print("Notion: 바뀐 것 없음")
        return
    print("Notion에서 가져올 내용:")
    for it in items:
        ref = f"#{it['id']} " if it.get("id") else ""
        line = f"  {it['no']:>2}. {PULL_LABEL.get(it['kind'], it['kind'])} {ref}{it['title']}"
        if it["kind"] in ("update", "conflict", "keep_local") and it.get("changes"):
            line += f": {'Notion 쪽 ' if it['kind'] != 'update' else ''}{_fmt_changes(it['changes'])}"
        elif it.get("fields") and it["kind"] in ("create", "skip") and _fmt_fields(it["fields"]):
            line += f" | {_fmt_fields(it['fields'])}"
        if it["kind"] == "skip" and it.get("reason"):
            line += f"  ({it['reason']})"
        print(line)


def _quick_drop(items: list[dict], text: str) -> list[dict] | None:
    """'2번 빼', '2, 3 제외', '-2' 같은 간단한 요청은 AI 없이 처리."""
    m = re.fullmatch(r"\s*-?\s*((?:\d+\s*(?:번)?\s*[,\s]\s*)*\d+)\s*(?:번)?\s*(?:빼(?:줘|고)?|제외(?:해줘)?|삭제|지워(?:줘)?)?\s*", text)
    if not m or not (text.strip().startswith("-") or re.search(r"빼|제외|삭제|지워", text)):
        return None
    drop = {int(x) for x in re.findall(r"\d+", m.group(1))}
    return [it for it in items if it["no"] not in drop]


def print_pull_result(st: dict) -> None:
    for i, t, ch in st["updated"]:
        print(f"← Notion 수정 #{i} {t}" + (f": {_fmt_changes(ch)}" if ch else ""))
    for i, t in st["created"]:
        print(f"← Notion 새 일정" + (f" #{i}" if i else "") + f" {t}")
    for i, t in st["deleted"]:
        print(f"← Notion에서 사라짐 → 로컬 삭제 #{i} {t}")
    for i, t in st.get("kept", []):
        print(f"↺ #{i} {t}: 로컬 값 유지 (다음 동기화 때 Notion을 덮음)")
    for i, t, ch in st["conflicts"]:
        print(f"! 충돌 #{i} {t}: 로컬과 Notion 둘 다 바뀜 → 로컬 유지 (Notion 쪽: {_fmt_changes(ch)})")
    for i, t, why in st["skipped"]:
        print(f"! 건너뜀 {('#' + str(i) + ' ') if i else ''}{t}: {why}")


def _notion_ready(cfg: Config, quiet: bool) -> bool:
    if cfg.notion.enabled and cfg.notion.token and cfg.notion.database_id:
        return True
    if not quiet:
        print("Notion이 꺼져 있음 — config.toml [notion] enabled/token/database_id 확인")
    return False


def run_pull(store: Store, cfg: Config, quiet: bool = False) -> bool:
    """Notion에서 고친 내용 바로 가져오기. 성공하면 True."""
    if not _notion_ready(cfg, quiet):
        return False
    try:
        st = sync_notion.pull(store, cfg.notion)
    except Exception as e:  # noqa: BLE001
        print(f"Notion 가져오기 실패: {e}")
        return False
    print_pull_result(st)
    if not quiet and not any(st.values()):
        print("Notion: 바뀐 것 없음")
    return True


def interactive_pull(store: Store, cfg: Config) -> bool:
    """미리보기 → [Y 적용 / n 취소 / 수정 요청] 반복. 적용하면 True."""
    if not _notion_ready(cfg, False):
        return False
    try:
        items, pages = sync_notion.plan_pull(store, cfg.notion)
    except Exception as e:  # noqa: BLE001
        print(f"Notion 가져오기 실패: {e}")
        return False
    while True:
        print_pull_preview(items)
        if not items:
            return False
        decision, text = ask_decision()
        if decision == "cancel":
            print("취소함. 로컬은 그대로.")
            return False
        if decision == "apply":
            st = sync_notion.apply_pull(store, items, pages)
            print_pull_result(st)
            return True
        quick = _quick_drop(items, text)
        if quick is not None:
            items = quick
            continue
        print("… 생각 중")
        try:
            reply, items = ai.revise_pull(items, text, cfg.claude)
        except (ai.ClaudeError, ValueError) as e:
            print(f"[오류] {e} — 목록은 그대로 둠")
            continue
        if reply:
            print(f"\n{reply}")


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
        if all(a["op"] == "plan" for a in plan.actions):  # 계획만 요청 → 바로 계획 단계로
            for eid, note in ai.resolve_plan_targets(store, plan.actions):
                cmd_plan(store, cfg, eid, assume_yes, note)
            history.append((msg, plan.reply + " [수행 일정 계획 진행]"))
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
            for eid, note in ai.resolve_plan_targets(store, plan.actions):
                cmd_plan(store, cfg, eid, assume_yes, note)
            history.append((msg, plan.reply + f" [적용: {', '.join(ok) or '없음'}]"))
            return
        if decision == "cancel":
            print("취소함.")
            history.append((msg, plan.reply + " [사용자가 적용 취소]"))
            return
        # revise → 같은 요청 + 직전 제안 + 수정 요청으로 다시 해석


def chat(store: Store, cfg: Config) -> None:
    print("Manager — 자연어로 일정 관리. 명령: /list /today /tags /teachers /plan <번호> /edit <번호> /pull /sync /quit")
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
            run_pull(store, cfg, quiet=True)
            run_sync(store, cfg)
        elif msg == "/pull":
            if interactive_pull(store, cfg):
                after_change(store, cfg)
        elif msg == "/tags":
            print_tags(store)
        elif msg == "/teachers":
            print_teachers(store)
        elif msg.startswith("/plan"):
            parts = msg.split(maxsplit=2)
            if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
                print("사용법: /plan <번호> [요청]")
            else:
                cmd_plan(store, cfg, int(parts[1].lstrip("#")), note=parts[2] if len(parts) > 2 else "")
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


# ---------------- 과제 수행 계획 ----------------
def school_cache(cfg: Config, refresh: bool = False, quiet: bool = True) -> dict:
    """학사일정 캐시. 없거나 7일 지났으면(또는 refresh) 학교 홈페이지에서 새로 받음."""
    path = school.cache_path((cfg.planner_path or cfg.db_path).parent)
    cache = school.load_cache(path)
    if refresh or school.is_stale(cache):
        try:
            evs = school.fetch_range(school.DEFAULT_URL, date.today().replace(day=1), 6)
            cache = school.save_cache(path, evs)
            if not quiet:
                print(f"학사일정 갱신: {len(evs)}건 ({path.name})")
        except Exception as e:  # noqa: BLE001
            print(f"(학사일정을 못 받아와서 {'저장된 것' if cache.get('events') else '미귀가 정보 없이'} 사용: {e})")
    return cache


def cmd_submit(store: Store, event_id: int, open_link: bool = True) -> None:
    ev = store.get(event_id)
    if not ev:
        raise KeyError(f"#{event_id} 일정 없음")
    if not ev.submit_link:
        print(f"#{ev.id} {ev.title}: 제출 경로 없음 — manager edit {ev.id} --submit <링크 또는 방법>")
        return
    print(f"#{ev.id} {ev.title}\n  제출: {ev.submit_link}")
    if open_link and ev.submit_link.startswith(("http://", "https://")):
        import webbrowser
        webbrowser.open(ev.submit_link)
        print("  (브라우저로 열었어)")


def cmd_school(cfg: Config, refresh: bool) -> None:
    cache = school_cache(cfg, refresh=refresh, quiet=False)
    stays = school.stay_dates(cache)
    t = date.today()
    print(f"\n[주말] (마지막 갱신 {cache.get('fetched_at') or '-'})")
    fri = t + timedelta(days=(4 - t.weekday()) % 7)
    for i in range(8):
        f = fri + timedelta(weeks=i)
        home = school.is_home_weekend_day(f, stays)
        print(f"  {f:%m/%d}(금)~{f + timedelta(days=1):%m/%d}(토)  {'귀가주 — 늦게까지 가능' if home else '미귀가주'}")
    print("\n[다가오는 학사일정]")
    for e in cache.get("events", []):
        if e["end"] >= t.isoformat() and e["start"] <= (t + timedelta(days=60)).isoformat():
            span = e["start"][5:] + (f"~{e['end'][5:]}" if e["end"] != e["start"] else "")
            print(f"  {span:<12} {e['title']}")


def cmd_plan(store: Store, cfg: Config, event_id: int, assume_yes: bool = False, note: str = "") -> None:
    profile = planner.load_profile(cfg.planner_path)
    if not profile.source:
        print(f"(planner.toml 없음 — 기본 시간표 사용. `manager profile`로 확인)")
    profile.stay_dates = school.stay_dates(school_cache(cfg))
    pending, feedback = None, note
    allow_q = True
    call = lambda p, files: ai.call_claude(p, cfg.claude, files)  # noqa: E731
    while True:
        print("… 수행 일정 계획 중")
        try:
            r = planner.make_plan(store, event_id, profile, call, pending, feedback, allow_questions=allow_q)
        except (ai.ClaudeError, KeyError, ValueError) as e:
            print(f"[오류] {e}")
            return
        if r.plan.get("reply"):
            print(f"\n{r.plan['reply']}")
        if r.questions:
            allow_q = False  # 질문은 한 번만, 그다음엔 가정해서 계획
            if assume_yes:
                continue
            answers = ask_plan_questions(r.questions)
            if answers:
                ev = store.get(event_id)
                store.update(event_id, scope="\n".join(x for x in [ev.scope, answers] if x))
                print("  (답변은 과제의 '범위·요구사항'에 저장했어)")
            continue
        print()
        for line in planner.describe(r):
            print(line)
        if not r.placed:
            print("\n넣을 수 있는 시간이 없어. planner.toml 가용 시간이나 마감을 확인해줘.")
        decision, fb = ("apply", "") if assume_yes else ask_decision()
        if decision == "apply" and r.placed:
            created = planner.apply_plan(store, r)
            print(f"  ✔ 작업 세션 {len(created)}개 추가 (#{created[0].id}~#{created[-1].id})")
            after_change(store, cfg)
            return
        if decision != "revise":
            print("취소함.")
            return
        pending, feedback = r.plan, fb


def ask_plan_questions(questions: list[str]) -> str:
    print("\n계획 전에 몇 가지만 알려줘 (엔터 = 모름/건너뛰기)")
    lines = []
    for q in questions:
        try:
            a = input(f"  {q} ").strip()
        except EOFError:
            a = ""
        if a:
            lines.append(f"{q} → {a}")
    return "\n".join(lines)


def cmd_profile(store: Store, cfg: Config) -> None:
    p = planner.load_profile(cfg.planner_path)
    print(f"설정 파일: {p.source or f'{cfg.planner_path} (없음 — 기본값 사용, planner.example.toml 참고)'}")
    print("\n[가용 시간]")
    for i, k in enumerate(planner.DAY_KEYS):
        w = ", ".join(f"{a:%H:%M}-{(b.strftime('%H:%M') if b else '24:00')}" for a, b in p.availability[k]) or "-"
        print(f"  {planner.WEEKDAYS[i]}  {w}")
    print(f"\n[세션] {p.min_minutes}~{p.max_minutes}분, 하루 과제 최대 {p.max_per_day_minutes}분, "
          f"매일 예비 {p.daily_reserve_minutes}분, 마감 전 여유 {p.buffer_hours:g}시간")
    fmt = lambda t: t.strftime("%H:%M") if t else "24:00"  # noqa: E731
    print(f"[늦은 시간] 평소 {fmt(p.latest_end)}까지 · 귀가주 금·토 {fmt(p.home_weekend_latest_end)}까지 "
          f"(`manager school`로 귀가주 확인)")
    print("\n[내 역량]  1 = 평균적인 고등학생 · 10 = 대학원생")
    if p.skills:
        for k, v in p.skills.items():
            print(f"  {k:<10} {'█' * v}{'·' * (10 - v)} {v}")
    else:
        print("  (planner.toml [skills]에 설정해줘)")
    calib = planner.calibration(store)
    comp = {k[3:]: v for k, v in calib.items() if k.startswith("요소:")}
    if comp:
        print("\n[작업 요소별 기록]  실제/예상")
        for k, v in comp.items():
            print(f"  {k:<10} ×{v['ratio']} ({v['n']}건)")
    print("\n[유형별 소요 시간]  기본 → 내 기록 반영")
    for k, v in p.estimates.items():
        c = calib.get(k)
        adj = f"→ {int(v * c['ratio'])}분 (기록 {c['n']}건, ×{c['ratio']})" if c else ""
        print(f"  {k:<8} {v}분 {adj}")
    if "전체" in calib:
        c = calib["전체"]
        print(f"\n전체 기록 {c['n']}건: 예상 {c['est']}분 → 실제 {c['actual']}분 (×{c['ratio']})")
    if p.note:
        print(f"\n[메모] {p.note}")


def cmd_done(store: Store, cfg: Config, ids: list[int], took: str | None) -> None:
    for i in ids:
        ev = store.get(i)
        if not ev:
            raise KeyError(f"#{i} 일정 없음")
        actual = planner.parse_duration(took) if took else None
        if actual is None and ev.est_minutes and not ev.parent_id and sys.stdin.isatty():
            done = sum(s.est_minutes or 0 for s in store.sessions_of(ev.id) if s.status == "done")
            hint = f"엔터 = 세션 합 {done}분" if done else "엔터 = 기록 안 함"
            try:
                v = input(f"#{i} 실제로 얼마나 걸렸어? (예: 3h, 2h30m / {hint}) ").strip()
            except EOFError:
                v = ""
            actual = planner.parse_duration(v) if v else None
        ev = ai.mark_done(store, i, actual)
        msg = f"완료 #{ev.id} {ev.title}"
        if ev.actual_minutes and ev.est_minutes:
            msg += f"  (예상 {ev.est_minutes}분 / 실제 {ev.actual_minutes}분 — 다음 추정에 반영)"
        print(msg)
    after_change(store, cfg)


def print_teachers(store: Store) -> None:
    rows = store.subject_teachers()
    if not rows:
        print("(등록된 과목 없음) — manager teacher set <과목> <선생님>")
        return
    for subj, t in rows:
        print(f"  {subj:<12} {t}")


def cmd_teacher(store: Store, cfg: Config, action: str, args: list[str]) -> None:
    if action == "list":
        print_teachers(store)
        return
    if action == "learn":
        cmd_teacher_learn(store, cfg)
        return
    if action == "set":
        if len(args) < 2:
            raise ValueError('사용법: manager teacher set <과목> <선생님>  (예: teacher set 화학 "서보배 선생님")')
        subject, teacher = args[0], " ".join(args[1:])
        n = store.set_subject_teacher(subject, teacher)
        print(f"{subject} → {teacher}" + (f"  (기존 Assignment {n}건에 자동 입력)" if n else ""))
    elif action == "rm":
        for subj in args:
            store.remove_subject_teacher(subj)
            print(f"삭제됨: {subj}")
    after_change(store, cfg)


def cmd_teacher_learn(store: Store, cfg: Config) -> None:
    """이미 적힌 일정에서 과목별 선생님을 추정 → 미리보기 → 확인 후 등록."""
    items = store.guess_subject_teachers()
    if not items:
        print("새로 등록할 과목이 없음 (선생님이 적힌 Assignment 일정 중 제목에서 과목을 찾은 게 없거나 이미 등록됨)")
        return
    for n, it in enumerate(items, 1):
        it["no"] = n
    while True:
        print("일정에서 찾은 과목별 선생님:")
        for it in items:
            line = f"  {it['no']:>2}. {it['subject']} → {it['teacher']}"
            if it["current"]:
                line += f"  (지금: {it['current']})"
            line += f"   ← {', '.join(it['events'])}"
            if it["others"]:
                line += f"  ※ 다른 선생님도 있음: {', '.join(it['others'])}"
            print(line)
        if not items:
            print("  (없음)")
            return
        try:
            ans = input("\n등록할까? [Y=등록 / n=취소 / '2번 빼' / '3번 생명과학' (과목명 바꾸기) / '1번 김OO 선생님'] ").strip()
        except EOFError:
            return
        if ans.lower() in YES:
            for it in items:
                k = store.set_subject_teacher(it["subject"], it["teacher"])
                print(f"등록: {it['subject']} → {it['teacher']}" + (f"  (Assignment {k}건 자동 입력)" if k else ""))
            after_change(store, cfg)
            return
        if ans.lower() in NO:
            print("취소함.")
            return
        quick = _quick_drop(items, ans)
        if quick is not None:
            items = quick
            continue
        m = re.fullmatch(r"\s*(\d+)\s*번?\s*(.+?)\s*", ans)
        target = next((it for it in items if m and it["no"] == int(m.group(1))), None)
        if not target:
            print("번호를 못 찾음 — 예: '2번 빼', '3번 생명과학', '1번 김OO 선생님'")
            continue
        value = m.group(2)
        if "선생님" in value or "쌤" in value or value in store.known_teachers():
            target["teacher"] = value.replace("쌤", "선생님")
        else:
            target["subject"] = value


EDIT_FIELDS = [  # (필드, 표시 이름)
    ("title", "제목"), ("start", "시작"), ("end", "종료 ('-' = 비우기)"), ("category", "태그"),
    ("subject", "과목"), ("teachers", "선생님 (쉼표로 여러 명, '-' = 비우기)"),
    ("team", "팀원 (쉼표로 여러 명, '-' = 비우기)"),
    ("topic", "주제"), ("scope", "범위·요구사항"), ("attachment", "안내문 파일 경로"),
    ("submit_link", "제출 (링크 또는 방법, 예: 드롭박스)"),
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
    if store.known_teachers():
        print("  선생님: " + ", ".join(store.known_teachers()))
    if store.known_members():
        print("  팀원: " + ", ".join(store.known_members()))
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
        elif key in ("teachers", "team", "subject", "topic", "scope", "attachment", "submit_link") and v == "-":
            fields[key] = ""
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
    print(f"수정됨 #{ev.id} {ev.title} ({ev.day} {ev.time_label()})" + (f" <{ev.category}>" if ev.category else "")
          + (f" 👤{ev.teachers}" if ev.teachers else ""))
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
    s.add_argument("-c", "--category", "--tag", dest="category", default="")
    s.add_argument("-t", "--teacher", action="append", default=[], help="담당 선생님 (여러 번 가능)")
    s.add_argument("-m", "--member", action="append", default=[], help="팀원 (여러 번 가능, 쉼표로 여러 명도 됨)")
    s.add_argument("-s", "--subject", default="", help="과목 (Assignment면 과목 선생님 자동 입력)")
    s.add_argument("--topic", default="", help="과제 주제")
    s.add_argument("--scope", default="", help="범위·요구사항 (분량, 형식, 산출물)")
    s.add_argument("--file", dest="attachment", default="", help="과제 안내문 파일 (PDF·이미지·txt)")
    s.add_argument("--submit", "--submit-link", dest="submit_link", default="",
                   help="제출 링크(URL), 링크가 없으면 방법 텍스트 (예: 드롭박스, 교무실 제출함)")
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
    s.add_argument("--took", help="실제 걸린 시간 (예: 3h, 2h30m, 90m) — 과제 시간 추정 보정에 사용")

    s = sub.add_parser("plan", help="과제 수행 일정(작업 세션) 짜기")
    s.add_argument("id", type=int)
    s.add_argument("note", nargs="*", help='요청 (예: "주말 위주로")')
    s.add_argument("--topic", help="주제 저장 후 계획")
    s.add_argument("--scope", help="범위·요구사항 저장 후 계획")
    s.add_argument("--file", dest="attachment", help="안내문 파일 저장 후 계획 (Claude가 직접 읽음)")
    s.add_argument("-y", "--yes", action="store_true", help="확인 없이 적용")

    sub.add_parser("profile", help="플래너 프로필(가용 시간·유형별 소요 시간·내 기록) 보기")

    s = sub.add_parser("submit", help="제출 경로 보기 (링크면 브라우저로 열기)")
    s.add_argument("id", type=int)
    s.add_argument("--no-open", action="store_true", help="브라우저로 열지 않음")

    s = sub.add_parser("school", help="학사일정·귀가주 확인 (학교 홈페이지)")
    s.add_argument("action", nargs="?", default="show", choices=["show", "sync"], help="sync = 지금 새로 받기")
    s = sub.add_parser("rm", help="삭제")
    s.add_argument("ids", type=int, nargs="+")

    s = sub.add_parser("edit", help="일정 수정 (번호만 주면 대화형, 문장을 주면 AI가 수정)")
    s.add_argument("id", type=int)
    s.add_argument("text", nargs="*", help='자연어 수정 요청, 예: "30분 늦춰줘"')
    for f in ("title", "start", "end", "location", "notes", "status"):
        s.add_argument(f"--{f}")
    s.add_argument("-c", "--category", "--tag", dest="category")
    s.add_argument("-t", "--teacher", dest="teachers", help="선생님 (쉼표로 여러 명, '-' = 비우기)")
    s.add_argument("-m", "--member", dest="team", help="팀원 (쉼표로 여러 명, '-' = 비우기)")
    s.add_argument("-s", "--subject", help="과목 ('-' = 비우기)")
    s.add_argument("--topic", help="과제 주제 ('-' = 비우기)")
    s.add_argument("--scope", help="범위·요구사항 ('-' = 비우기)")
    s.add_argument("--file", dest="attachment", help="과제 안내문 파일 경로 ('-' = 비우기)")
    s.add_argument("--submit", "--submit-link", dest="submit_link", help="제출 링크 또는 방법 ('-' = 비우기)")
    s.add_argument("--deadline", dest="deadline", action="store_true", default=None, help="마감 일정으로")
    s.add_argument("--no-deadline", dest="deadline", action="store_false", help="마감 일정 해제")

    s = sub.add_parser("teacher", help="과목별 선생님 (Assignment 자동 입력)")
    s.add_argument("action", nargs="?", default="list", choices=["list", "set", "rm", "learn"])
    s.add_argument("args", nargs="*")

    s = sub.add_parser("tag", help="태그(카테고리) 관리")
    s.add_argument("action", nargs="?", default="list", choices=["list", "add", "rename", "rm"])
    s.add_argument("names", nargs="*")
    s.add_argument("--to", dest="move_to", default="", help="rm 할 때 일정을 옮길 태그")

    s = sub.add_parser("sync", help="Notion/Obsidian 백업")
    s.add_argument("--notion", action="store_true", help="Notion만")
    s.add_argument("--obsidian", action="store_true", help="Obsidian만")
    s.add_argument("--no-pull", action="store_true", help="Notion에서 가져오기 생략 (내보내기만)")

    s = sub.add_parser("pull", help="Notion에서 고친 내용을 로컬에 반영")
    s.add_argument("--dry-run", action="store_true", help="미리보기 → 적용(Y)/취소(n)/고칠 내용 입력 반복")

    s = sub.add_parser("notion-setup", help="Notion에 일정 DB 생성")
    s.add_argument("parent_page_id", help="DB를 만들 부모 페이지 ID (integration이 공유돼 있어야 함)")

    s = sub.add_parser("init", help="설정 파일 템플릿 생성")
    s.add_argument("--path", default=str(Path.home() / ".manager" / "config.toml"))
    return p


# 시작할 때 Notion 변경분을 먼저 가져오는 명령
AUTO_PULL = {"chat", "ask", "add", "list", "today", "done", "plan", "submit", "rm", "edit", "teacher"}


def _split_args(values: list[str]) -> list[str]:
    """-t A -t "B, C" → [A, B, C]"""
    out: list[str] = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


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

    if cmd in AUTO_PULL and cfg.notion.pull and cfg.notion.enabled:
        run_pull(store, cfg, quiet=True)

    try:
        if cmd == "chat":
            chat(store, cfg)
        elif cmd == "ask":
            handle_message(store, cfg, " ".join(args.text), [], assume_yes=args.yes)
        elif cmd == "add":
            ev = store.add(args.title, args.start, args.end, args.all_day, args.category, args.location, args.notes,
                           deadline=args.deadline, teachers=_split_args(args.teacher), subject=args.subject,
                           team=_split_args(args.member))
            extra = {k: v for k in ("topic", "scope", "attachment", "submit_link") if (v := getattr(args, k))}
            if extra:
                ev = store.update(ev.id, **extra)
            print(f"추가됨 #{ev.id} {ev.title} ({ev.day} {ev.time_label()})" + (f" 👤{ev.teachers}" if ev.teachers else ""))
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
            cmd_done(store, cfg, args.ids, args.took)
        elif cmd == "plan":
            pre = {k: v for k in ("topic", "scope", "attachment") if (v := getattr(args, k))}
            if pre:
                store.update(args.id, **pre)
            cmd_plan(store, cfg, args.id, args.yes, " ".join(args.note))
        elif cmd == "profile":
            cmd_profile(store, cfg)
        elif cmd == "submit":
            cmd_submit(store, args.id, not args.no_open)
        elif cmd == "school":
            cmd_school(cfg, refresh=args.action == "sync")
        elif cmd == "rm":
            for i in args.ids:
                print("삭제", store.delete(i).title)
            after_change(store, cfg)
        elif cmd == "edit":
            fields = {k: v for k in ("title", "start", "end", "category", "location", "notes", "status", "deadline",
                                     "teachers", "team", "subject", "topic", "scope", "attachment", "submit_link")
                      if (v := getattr(args, k)) is not None}
            if fields.get("end") == "-":
                fields["end"] = None
            for k in ("teachers", "team", "subject", "topic", "scope", "attachment", "submit_link"):
                if fields.get(k) == "-":
                    fields[k] = ""
            cmd_edit(store, cfg, args.id, " ".join(args.text), fields)
        elif cmd == "teacher":
            cmd_teacher(store, cfg, args.action, args.args)
        elif cmd == "tag":
            cmd_tag(store, cfg, args.action, args.names, args.move_to)
        elif cmd == "pull":
            if (interactive_pull(store, cfg) if args.dry_run else run_pull(store, cfg)):
                after_change(store, cfg)
        elif cmd == "sync":
            both = not (args.notion or args.obsidian)
            if (both or args.notion) and not args.no_pull:
                run_pull(store, cfg, quiet=True)
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
