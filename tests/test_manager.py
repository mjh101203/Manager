import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from manager import ai, school, sync_notion, sync_obsidian
from manager.config import ClaudeConfig, NotionConfig, ObsidianConfig
from manager.db import Store, normalize_time

KST = timezone(timedelta(hours=9))


@pytest.fixture
def store():
    return Store(":memory:")


# ---------- DB ----------
def test_normalize():
    assert normalize_time("2026-10-28", True) == "2026-10-28"
    assert normalize_time("2026-10-28 15:00", False).startswith("2026-10-28T15:00:00")
    assert normalize_time("2026-10-28T15:00+09:00", False) == "2026-10-28T15:00:00+09:00"
    assert normalize_time("", False) is None


def test_crud_and_range(store):
    a = store.add("물포럼 회의", "2026-10-28 15:00", "2026-10-28 16:00", category="물포럼")
    b = store.add("코드페어 마감", "2026-10-30", all_day=True, category="대회")
    assert a.time_label() == "15:00–16:00" and b.time_label() == "종일"
    assert [e.id for e in store.range("2026-10-28", "2026-10-29")] == [a.id]
    assert [e.id for e in store.range("2026-10-28", "2026-10-31")] == [a.id, b.id]
    store.update(a.id, status="done")
    assert store.range("2026-10-28", "2026-10-31", include_done=False)[0].id == b.id
    store.delete(b.id)
    assert store.get(b.id) is None and store.get(b.id, include_deleted=True).deleted
    with pytest.raises(ValueError):
        store.add("x", "2026-10-28 15:00", "2026-10-28 14:00")
    with pytest.raises(ValueError):
        store.update(a.id, status="weird")
    assert store.categories() == ["대회", "물포럼"]  # 일정이 지워져도 태그는 남음


def test_all_day_toggle(store):
    e = store.add("x", "2026-10-28 15:00")
    e = store.update(e.id, all_day=True)
    assert e.start == "2026-10-28" and e.all_day


# ---------- AI ----------
def test_prompt_has_calendar_and_events(store):
    store.add("물포럼 회의", "2026-10-28 15:00")
    p = ai.build_prompt(store, "내일 일정 뭐야", now=datetime(2026, 10, 27, 17, 0, tzinfo=KST))
    assert "2026-10-28 수 (내일)" in p
    assert "#1 [todo] 2026-10-28(수) 15:00 물포럼 회의" in p
    assert "내일 일정 뭐야" in p


def test_parse_plan_variants():
    raw = '{"reply":"추가할게","actions":[{"op":"add","title":"a","start":"2026-10-28T10:00"},{"op":"bogus"}]}'
    assert len(ai.parse_plan(raw).actions) == 1
    assert ai.parse_plan("```json\n" + raw + "\n```").reply == "추가할게"
    assert ai.parse_plan("앞말 " + raw + " 뒷말").actions[0]["title"] == "a"
    plain = ai.parse_plan("날짜가 언제야?")
    assert plain.actions == [] and plain.reply == "날짜가 언제야?"


def test_apply_actions(store):
    e = store.add("기존", "2026-10-28 09:00")
    ok, fail = ai.apply_actions(store, [
        {"op": "add", "title": "새거", "start": "2026-10-29T10:00", "end": None, "all_day": False},
        {"op": "update", "id": e.id, "fields": {"start": "2026-10-28T11:00", "notion_page_id": "hack"}},
        {"op": "done", "id": 999},
    ])
    assert len(ok) == 2 and len(fail) == 1
    assert store.get(e.id).start.startswith("2026-10-28T11:00")
    assert store.get(e.id).notion_page_id is None  # 허용 안 된 필드는 무시


@pytest.mark.skipif(sys.platform == "win32", reason="shebang 스크립트 사용")
def test_call_claude_with_fake_cli(tmp_path, store):
    """`claude -p --output-format json` 출력 형식을 흉내내는 가짜 CLI로 전체 경로 검증."""
    plan = {"reply": "추가했어", "actions": [{"op": "add", "title": "테스트", "start": "2026-11-01", "all_day": True}]}
    fake = tmp_path / "fakeclaude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, json\n"
        "prompt = sys.stdin.read()\n"
        "assert '사용자 요청' in prompt and sys.argv[1:4] == ['-p', '--output-format', 'json']\n"
        f"print(json.dumps({{'type': 'result', 'is_error': False, 'result': {json.dumps(json.dumps(plan, ensure_ascii=False))}}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    p = ai.interpret(store, "11월 1일 테스트 추가", ClaudeConfig(command=str(fake)))
    assert p.reply == "추가했어" and p.actions[0]["title"] == "테스트"


def test_call_claude_missing_binary():
    with pytest.raises(ai.ClaudeError):
        ai.call_claude("hi", ClaudeConfig(command="definitely-not-a-real-claude-binary"))


# ---------- Obsidian ----------
def test_obsidian_sync(tmp_path, store):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = ObsidianConfig(enabled=True, vault_path=str(vault), folder="Manager")
    a = store.add("물포럼 회의", "2026-10-28 15:00", category="물 포럼", notes="자료 챙기기")
    store.add("마감", "2026-10-30", all_day=True)
    s = sync_obsidian.sync(store, cfg, today=date(2026, 10, 27))
    daily = vault / "Manager" / "Daily" / "2026-10-28.md"
    text = daily.read_text(encoding="utf-8")
    assert "generator: manager" in text and "**물포럼 회의**" in text and "#물_포럼" in text
    assert "    자료 챙기기" in text
    assert (vault / "Manager" / "Index.md").exists() and s["written"] == 3
    # 재실행 시 변경 없음
    assert sync_obsidian.sync(store, cfg, today=date(2026, 10, 27))["written"] == 0
    # 삭제하면 해당 날짜 노트 제거
    store.delete(a.id)
    assert sync_obsidian.sync(store, cfg, today=date(2026, 10, 27))["removed"] == 1
    assert not daily.exists()
    # 사용자가 직접 만든 파일은 안 건드림
    mine = vault / "Manager" / "Daily" / "2026-10-30.md"
    mine.write_text("내 메모", encoding="utf-8")
    with pytest.raises(RuntimeError):
        sync_obsidian.sync(store, cfg, today=date(2026, 10, 27))


# ---------- Notion ----------
class FakeNotion:
    def __init__(self):
        self.calls = []
        self.n = 0
        self.missing = set()
        self.db_calls = []
        self.options = [{"id": "opt-a", "name": "학교"}]
        self.page_cat = {}  # 페이지 id → Category (Notion 쪽 상태)

    def request(self, method, path, body=None, retries=3):
        if path.endswith("/query"):  # Category 필터 검색
            name = body["filter"]["select"]["equals"]
            return {"results": [{"id": pid} for pid, c in self.page_cat.items() if c == name], "has_more": False}
        if method == "PATCH" and path.startswith("/pages/") and body and "Category" in body.get("properties", {}):
            sel = body["properties"]["Category"]["select"]
            self.page_cat[path.split("/")[-1]] = sel["name"] if sel else None
        if path.startswith("/databases"):  # 스키마(태그 선택지) 호출은 따로 기록
            self.db_calls.append((method, body))
            if method == "PATCH":
                opts = body["properties"]["Category"]["select"]["options"]
                by_id = {o["id"]: o for o in self.options}
                self.options = [by_id[o["id"]] if "id" in o else {"id": f"opt-{o['name']}", "name": o["name"]}
                                for o in opts]
            return {"properties": {"Category": {"type": "select", "select": {"options": self.options}}}}
        self.calls.append((method, path, body))
        if method == "PATCH" and path.split("/")[-1] in self.missing:
            raise sync_notion.NotionError(f"{method} {path} → HTTP 404: not found")
        if method == "POST" and path == "/pages":
            self.n += 1
            return {"id": f"page-{self.n}"}
        return {}


def test_notion_sync(store):
    cfg = NotionConfig(enabled=True, token="x", database_id="db")
    fake = FakeNotion()
    a = store.add("A", "2026-10-28 15:00", "2026-10-28 16:00", category="학교")
    b = store.add("B", "2026-10-29", all_day=True)
    s = sync_notion.sync(store, cfg, fake)
    assert s["created"] == 2 and not s["errors"]
    props = fake.calls[0][2]["properties"]
    assert props["Start Date"]["date"]["start"].startswith("2026-10-28T15:00")
    assert props["End Date"]["date"]["start"].startswith("2026-10-28T16:00")
    assert props["Done"]["checkbox"] is False
    assert set(props) == {"Name", "Start Date", "End Date", "Done", "Category", "Teacher", "Team", "Submission Link"}  # Notion 전용 칸은 건드리지 않음
    # 변경 없으면 아무 호출 없음
    fake.calls.clear()
    assert sync_notion.sync(store, cfg, fake)["created"] == 0 and fake.calls == []
    # 수정 → update, 삭제 → archive
    store.update(a.id, status="done")
    store.delete(b.id)
    s = sync_notion.sync(store, cfg, fake)
    assert s["updated"] == 1 and s["archived"] == 1
    assert store.get(b.id, include_deleted=True).notion_page_id is None
    # Notion에서 페이지가 사라졌으면 재생성
    fake.missing.add(store.get(a.id).notion_page_id)
    store.update(a.id, title="A2")
    s = sync_notion.sync(store, cfg, fake)
    assert s["created"] == 1 and store.get(a.id).notion_page_id == "page-3"


def test_cli_smoke(tmp_path, capsys, monkeypatch):
    from manager.cli import main
    cfgp = tmp_path / "config.toml"
    cfgp.write_text('db_path = "t.db"\nauto_sync = false\n', encoding="utf-8")
    assert main(["--config", str(cfgp), "add", "회의", "2026-10-28 15:00", "-c", "학교"]) == 0
    assert main(["--config", str(cfgp), "list", "--from", "2026-10-28", "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "회의" in out and "<학교>" in out and "2026-10-28 (수)" in out
    assert main(["--config", str(cfgp), "done", "1"]) == 0
    assert main(["--config", str(cfgp), "rm", "99"]) == 1


def test_deadline_goes_to_end_date(store):
    d = store.add("수행평가", "2026-11-12", all_day=True, deadline=True)
    assert d.time_label() == "마감"
    props = sync_notion.event_properties(d)
    assert props["Start Date"]["date"]["start"] == date.today().isoformat()  # 입력한 날짜
    assert props["End Date"]["date"]["start"] == "2026-11-12"
    r = store.add("대회", "2026-06-02", "2026-11-09", all_day=True)
    props = sync_notion.event_properties(r)
    assert props["Start Date"]["date"]["start"] == "2026-06-02" and props["End Date"]["date"]["start"] == "2026-11-09"


def test_migration_adds_deadline(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, start TEXT NOT NULL,
        end TEXT, all_day INTEGER NOT NULL DEFAULT 0, category TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'todo', deleted INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, notion_page_id TEXT, notion_synced_at TEXT);
        INSERT INTO events (title, start, created_at, updated_at) VALUES ('x', '2026-01-01', 't', 't');""")
    c.commit(); c.close()
    s = Store(p)
    assert s.get(1).deadline is False


def test_tags(store):
    store.add("A", "2026-11-01", all_day=True, category="학교")
    assert store.add_tag("Club") is True and store.add_tag("Club") is False
    assert store.tags() == [("Club", 0), ("학교", 1)]
    assert store.rename_tag("학교", "School") == 1
    assert store.get(1).category == "School" and not store.has_tag("학교")
    assert store.remove_tag("School", move_to="Club") == 1
    assert store.get(1).category == "Club" and store.tags() == [("Club", 1)]
    with pytest.raises(KeyError):
        store.rename_tag("없는태그", "x")


def test_notion_adds_missing_tag_options(store):
    cfg = NotionConfig(enabled=True, token="x", database_id="db")
    fake = FakeNotion()
    store.add_tag("Research")  # 일정 없는 새 태그
    store.add("A", "2026-11-01", all_day=True, category="학교")
    s = sync_notion.sync(store, cfg, fake)
    assert s["new_tags"] == ["Research"]
    patch = [b for m, b in fake.db_calls if m == "PATCH"][0]
    assert {"id": "opt-a"} in patch["properties"]["Category"]["select"]["options"]  # 기존 옵션 유지
    assert sync_notion.sync(store, cfg, fake)["new_tags"] == []


def _cli_env(tmp_path):
    from manager.config import Config
    from manager.db import Store
    return Store(tmp_path / "t.db"), Config(db_path=tmp_path / "t.db", auto_sync=False)


def test_revise_loop(tmp_path, monkeypatch, capsys):
    """수정 요청 입력 → 새 제안 → Y 로 적용. AI에는 직전 제안과 수정 요청이 전달돼야 함."""
    from manager import cli
    store, cfg = _cli_env(tmp_path)
    calls = []

    def fake_interpret(store_, msg, ccfg, hist, pending=None, feedback=""):
        calls.append((pending, feedback))
        if not feedback:
            return ai.Plan("두 개 추가할게", [
                {"op": "add", "title": "보고서", "start": "2026-11-16T21:00", "deadline": True},
                {"op": "add", "title": "회의", "start": "2026-11-17T16:00"}])
        assert pending and len(pending.actions) == 2 and feedback == "2번 빼줘"
        return ai.Plan("보고서만 추가할게", [pending.actions[0]])

    answers = iter(["2번 빼줘", ""])
    monkeypatch.setattr(ai, "interpret", fake_interpret)
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    cli.handle_message(store, cfg, "보고서랑 회의", [])
    assert [e.title for e in store.all()] == ["보고서"] and store.get(1).deadline
    assert len(calls) == 2


def test_revise_typed_directly_and_cancel(tmp_path, monkeypatch):
    from manager import cli
    store, cfg = _cli_env(tmp_path)
    seen = []

    def fake_interpret(store_, msg, ccfg, hist, pending=None, feedback=""):
        seen.append(feedback)
        return ai.Plan("ok", [{"op": "add", "title": "x", "start": "2026-11-16"}])

    answers = iter(["시간 8시로", "n"])  # 수정 입력 → 다시 제안 → n = 바로 취소
    monkeypatch.setattr(ai, "interpret", fake_interpret)
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    cli.handle_message(store, cfg, "x 추가", [])
    assert seen == ["", "시간 8시로"] and store.all() == []


def test_edit_interactive_and_flags(tmp_path, monkeypatch):
    from manager import cli
    store, cfg = _cli_env(tmp_path)
    store.add("보고서", "2026-11-16", all_day=True, category="Assignment")
    # 제목·태그·deadline만 바꾸고 나머지는 엔터
    # 제목, 시작, 종료, 태그, 과목, 선생님, 팀원, 주제, 범위, 파일, 제출, 장소, 메모, 마감, 상태, (새 태그 확인)
    answers = iter(["대수 보고서 제출", "", "", "Research", "", "", "김헨리, 김준식", "해밍 코드", "", "",
                    "https://classroom.google.com/c/abc", "", "", "y", "", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    cli.cmd_edit(store, cfg, 1, "", {})
    e = store.get(1)
    assert (e.title, e.category, e.deadline, e.topic) == ("대수 보고서", "Research", True, "해밍 코드")
    assert e.submit_link == "https://classroom.google.com/c/abc"
    assert e.team_list == ["김헨리", "김준식"]  # 제목의 '제출'은 위에서 지워져서 "대수 보고서"
    cli.main(["--config", str(_write_cfg(tmp_path)), "edit", "1", "--end", "2026-11-20", "--no-deadline"])


def _write_cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('db_path = "t.db"\nauto_sync = false\n', encoding="utf-8")
    return p


def test_cli_tag_commands(tmp_path, capsys):
    from manager.cli import main
    cfgp = _write_cfg(tmp_path)
    assert main(["--config", str(cfgp), "tag", "add", "Club"]) == 0
    assert main(["--config", str(cfgp), "tag", "rename", "Club", "Durunuri"]) == 0
    assert main(["--config", str(cfgp), "tag"]) == 0
    out = capsys.readouterr().out
    assert "Durunuri" in out and "Club → Durunuri" in out
    assert main(["--config", str(cfgp), "tag", "rm", "없는거"]) == 1


def test_tag_ops_applied_before_events(store):
    ok, fail = ai.apply_actions(store, [
        {"op": "add", "title": "A", "start": "2026-11-01", "category": "Club"},
        {"op": "add_tag", "name": "Club"},
    ])
    assert not fail and ok[0].startswith("태그 추가됨") and store.get(1).category == "Club"
    assert "(새 태그)" in ai.describe_action(store, {"op": "add", "title": "B", "start": "x", "category": "New"})


def test_tag_rename_updates_every_notion_page(store):
    """Manager가 모르는 Notion 페이지까지 바꾸고, 예전 선택지는 지움."""
    cfg = NotionConfig(enabled=True, token="x", database_id="db")
    fake = FakeNotion()
    fake.options = [{"id": "opt-c", "name": "Competition"}, {"id": "opt-r", "name": "Research"}]
    store.add_tag("Research")
    store.add("KJWP", "2026-06-02", "2026-11-09", all_day=True, category="Competition")
    sync_notion.sync(store, cfg, fake)
    fake.page_cat = {"page-1": "Competition", "notion-only-KSEF": "Competition", "other": "Research"}

    store.rename_tag("Competition", "Contest")
    s = sync_notion.sync(store, cfg, fake)
    assert not s["errors"] and s["retagged"] == 2
    assert fake.page_cat == {"page-1": "Contest", "notion-only-KSEF": "Contest", "other": "Research"}
    names = {o["name"] for o in fake.options}
    assert "Competition" not in names and {"Contest", "Research"} <= names
    assert store.pending_tag_ops() == []
    assert sync_notion.sync(store, cfg, fake)["retagged"] == 0  # 한 번만 적용

    store.remove_tag("Research")  # 삭제 → 그 태그 페이지는 비움
    s = sync_notion.sync(store, cfg, fake)
    assert fake.page_cat["other"] is None and "Research" not in {o["name"] for o in fake.options}


def test_subject_teacher_autofill(store):
    store.add("화학 독서감상문", "2026-09-19", all_day=True, category="Assignment")  # 등록 전 → 빈칸
    assert store.get(1).teachers == ""
    assert store.set_subject_teacher("화학", "서보배 선생님") == 1  # 기존 Assignment도 채움
    assert store.get(1).teacher_list == ["서보배 선생님"] and store.get(1).subject == "화학"
    store.set_subject_teacher("대수", "정도일 선생님")
    # 새 Assignment: 제목으로 과목 판별
    e = store.add("대수 주제탐구 보고서", "2026-11-16T21:00", category="Assignment", deadline=True)
    assert e.teacher_list == ["정도일 선생님"] and e.subject == "대수"
    # 다른 태그면 자동 입력 안 함
    assert store.add("대수 경시대회", "2026-12-01", all_day=True, category="Competition").teachers == ""
    # 직접 지정한 선생님이 우선
    e = store.add("화학 실험보고서", "2026-11-20", all_day=True, category="Assignment", teachers=["이설아 선생님"])
    assert e.teacher_list == ["이설아 선생님"]
    # 나중에 Assignment로 바꾸면 그때 채움
    e = store.add("대수 문제풀이", "2026-11-21", all_day=True)
    assert store.update(e.id, category="Assignment").teacher_list == ["정도일 선생님"]
    # Notion에 Teacher로 나감
    props = sync_notion.event_properties(store.get(2))
    assert props["Teacher"]["multi_select"] == [{"name": "정도일 선생님"}]
    assert "서보배 선생님" in store.known_teachers()


def test_ai_add_with_subject_preview(store):
    store.set_subject_teacher("물리", "이누리 선생님")
    a = {"op": "add", "title": "탐구보고서", "start": "2026-11-20", "all_day": True,
         "category": "Assignment", "subject": "물리"}
    assert "👤이누리 선생님 (자동)" in ai.describe_action(store, a)
    ok, fail = ai.apply_actions(store, [a])
    assert not fail and store.get(1).teacher_list == ["이누리 선생님"]


def test_cli_teacher_commands(tmp_path, capsys):
    from manager.cli import main
    cfgp = _write_cfg(tmp_path)
    assert main(["--config", str(cfgp), "teacher", "set", "화학", "서보배", "선생님"]) == 0
    assert main(["--config", str(cfgp), "add", "화학 보고서", "2026-11-20", "--all-day", "-c", "Assignment"]) == 0
    assert main(["--config", str(cfgp), "edit", "1", "--teacher", "이설아 선생님, 서보배 선생님"]) == 0
    assert main(["--config", str(cfgp), "teacher"]) == 0
    out = capsys.readouterr().out
    assert "👤서보배 선생님" in out and "👤이설아 선생님, 서보배 선생님" in out and "화학" in out


def test_obsidian_nav_links_only_to_existing_days(tmp_path, store):
    vault = tmp_path / "v"
    vault.mkdir()
    store.add("A", "2026-11-06T13:30", category="Assignment", deadline=True)
    store.add("B", "2026-11-14", all_day=True, deadline=True)
    store.add("C", "2026-11-16", all_day=True)
    assert store.get(1).time_label() == "13:30 마감"
    sync_obsidian.sync(store, ObsidianConfig(True, str(vault), "Manager"), today=date(2026, 10, 28))
    mid = (vault / "Manager" / "Daily" / "2026-11-14.md").read_text(encoding="utf-8")
    assert "[[Manager/Daily/2026-11-06|2026-11-06]]" in mid and "[[Manager/Daily/2026-11-16|2026-11-16]]" in mid
    assert "2026-11-13" not in mid and "2026-11-15" not in mid
    first = (vault / "Manager" / "Daily" / "2026-11-06.md").read_text(encoding="utf-8")
    assert "←" not in first and "[[Manager/Index|" in first
    idx = (vault / "Manager" / "Index.md").read_text(encoding="utf-8")
    assert "[[Manager/Daily/2026-11-06|" in idx


# ---------- 과제 수행 플래너 ----------
from manager import planner as pl


def _profile(**kw):
    p = pl.load_profile(None)
    p.latest_end = None  # 기존 테스트는 늦은 시간 제한 없이 (제한은 test_latest_end_* 에서)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _kst(s):
    return datetime.fromisoformat(s).replace(tzinfo=KST)


def test_free_slots_subtract_busy():
    p = _profile()
    busy = [(_kst("2026-11-02T20:00"), _kst("2026-11-02T21:00"))]  # 월 20~21 바쁨
    slots = pl.free_slots(p, busy, _kst("2026-11-02T00:00"), _kst("2026-11-03T00:00"))
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in slots] == [("19:30", "20:00"), ("21:00", "23:00")]


def test_chunks_split_and_merge():
    p = _profile()
    ch = pl.make_chunks([{"name": "조사", "minutes": 150}, {"name": "개요", "minutes": 20}, {"name": "초안", "minutes": 60}], p)
    assert [(c.name, c.minutes) for c in ch] == [("조사 (1/2)", 75), ("조사 (2/2) + 개요", 95), ("초안", 60)]


def test_pack_respects_daily_cap_order_and_spread():
    p = _profile(max_per_day_minutes=120)
    slots = pl.free_slots(p, [], _kst("2026-11-02T00:00"), _kst("2026-11-09T00:00"))  # 월~일
    chunks = [pl.Chunk(f"s{i}", 90) for i in range(4)]
    placed, failed = pl.pack(chunks, slots, p, {"spread": "even"})
    assert not failed and [x.name for x in placed] == ["s0", "s1", "s2", "s3"]
    days = [x.start.date() for x in placed]
    assert days == sorted(days) and len(set(days)) == 4  # 고르게 → 날마다 하나
    assert all(a.end <= b.start for a, b in zip(placed, placed[1:]))
    placed2, _ = pl.pack(chunks, slots, p, {"spread": "even", "prefer": "weekend"})
    assert all(x.start.weekday() >= 5 for x in placed2[:2])


def test_pack_reports_shortage():
    p = _profile(daily_reserve_minutes=0)
    slots = pl.free_slots(p, [], _kst("2026-11-02T19:00"), _kst("2026-11-02T23:00"))
    placed, failed = pl.pack([pl.Chunk("a", 120), pl.Chunk("b", 120)], slots, p)
    assert [x.name for x in placed] == ["a", "b"] and [(c.name, c.minutes) for c in failed] == [("b", 60)]


def test_make_plan_apply_replan_and_calibrate(store):
    p = _profile()
    a = store.add("대수 주제탐구 보고서", "2026-11-16T21:00", category="Assignment", deadline=True)
    store.add("두루누리 회의", "2026-11-10T19:30", "2026-11-10T21:30")  # 이 시간은 피해야 함
    seen = []

    def fake_ai(prompt, files=None):
        seen.append(prompt)
        assert "과제 수행 계획 규칙" in prompt and "대수 주제탐구 보고서" in prompt
        return json.dumps({"reply": "보통 난이도", "task_type": "보고서", "complexity": 3, "total_minutes": 240,
                           "steps": [{"name": "자료 조사", "minutes": 60}, {"name": "개요", "minutes": 30},
                                     {"name": "초안", "minutes": 90}, {"name": "수정", "minutes": 60}],
                           "strategy": {"spread": "even"}})

    now = _kst("2026-11-09T12:00")
    r = pl.make_plan(store, a.id, p, fake_ai, now=now)
    assert not r.failed and sum(x.minutes for x in r.placed) == 240
    due = _kst("2026-11-16T21:00") - timedelta(hours=12)
    assert all(x.end <= due for x in r.placed)
    meeting = (_kst("2026-11-10T19:30"), _kst("2026-11-10T21:30"))
    assert all(x.end <= meeting[0] or x.start >= meeting[1] for x in r.placed)
    created = pl.apply_plan(store, r)
    assert all(s.parent_id == a.id and s.category == "Assignment" for s in created)
    assert store.get(a.id).est_minutes == 240 and store.get(a.id).task_type == "보고서"
    # 첫 세션 완료 후 다시 계획 → 남은 예정 세션만 교체, 한 분량은 프롬프트에 전달
    store.update(created[0].id, status="done")
    r2 = pl.make_plan(store, a.id, p, fake_ai, now=now)
    assert set(r2.replace_ids) == {s.id for s in created[1:]} and r2.done_minutes == created[0].est_minutes
    assert f"이미 끝낸 작업 세션: {created[0].est_minutes}분" in seen[-1]
    # 완료 + 실제 시간 기록 → 보정 비율
    ev = ai.mark_done(store, a.id, 360)
    assert ev.actual_minutes == 360
    assert all(s.status == "done" for s in store.sessions_of(a.id))  # 남은 예정 세션은 정리됨
    c = pl.calibration(store)
    assert c["보고서"]["ratio"] == 1.5
    assert pl.suggested_minutes(p, c, "보고서", 3) == 360


def test_parse_duration():
    assert pl.parse_duration("3h") == 180 and pl.parse_duration("2h30m") == 150
    assert pl.parse_duration("90") == 90 and pl.parse_duration("1.5시간") == 90


def test_profile_toml(tmp_path):
    f = tmp_path / "planner.toml"
    f.write_text('[availability]\nmon = "20:00-24:00"\n[estimates]\n"보고서" = 300\n[me]\nnote = "글쓰기 느림"\n',
                 encoding="utf-8")
    p = pl.load_profile(f)
    assert p.availability["mon"][0][1] is None and p.estimates["보고서"] == 300 and p.estimates["발표"] == 180
    assert p.note == "글쓰기 느림"


def test_daily_reserve_left_free():
    """매일 예비 시간(기본 120분)은 비워둠: 평일 19:30~23:00(210분)이면 과제는 최대 90분."""
    p = _profile()
    slots = pl.free_slots(p, [], _kst("2026-11-02T00:00"), _kst("2026-11-03T00:00"))  # 월요일 하루
    placed, failed = pl.pack([pl.Chunk("a", 60), pl.Chunk("b", 60)], slots, p)
    assert [x.name for x in placed] == ["a"] and [c.name for c in failed] == ["b"]
    p2 = _profile(daily_reserve_minutes=0)
    placed, failed = pl.pack([pl.Chunk("a", 60), pl.Chunk("b", 60)], slots, p2)
    assert not failed and sum(x.minutes for x in placed) == 120


def test_questions_and_components_calibration(store):
    p = _profile()
    a = store.add("지구시스템 탐구 프로젝트", "2026-11-12", all_day=True, category="Assignment", deadline=True)
    calls = []

    def ask_first(prompt, files=None):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps({"reply": "주제가 없어", "questions": ["주제가 뭐야?"], "steps": []})
        return json.dumps({"reply": "ok", "task_type": "보고서", "complexity": 4, "total_minutes": 300,
                           "components": [{"name": "딥러닝", "level_needed": 6, "minutes": 200},
                                          {"name": "글쓰기", "level_needed": 3, "minutes": 100}],
                           "steps": [{"name": "모델 학습", "minutes": 200}, {"name": "보고서", "minutes": 100}]})

    now = _kst("2026-10-29T12:00")
    r = pl.make_plan(store, a.id, p, ask_first, now=now)
    assert r.questions == ["주제가 뭐야?"] and not r.placed
    store.update(a.id, topic="CNN+BiLSTM P파 검출")
    r = pl.make_plan(store, a.id, p, ask_first, now=now)
    assert "CNN+BiLSTM P파 검출" in calls[-1] and not r.questions and r.placed
    pl.apply_plan(store, r)
    ai.mark_done(store, a.id, 450)  # 예상 300 → 실제 450
    c = pl.calibration(store)
    assert c["요소:딥러닝"]["ratio"] == 1.5 and c["요소:글쓰기"]["est"] == 100


def test_attachment_passed_to_ai(store, tmp_path):
    f = tmp_path / "안내문.txt"
    f.write_text("A4 3장, 수식 포함", encoding="utf-8")
    a = store.add("대수 보고서", "2026-11-16", all_day=True, category="Assignment", deadline=True)
    store.update(a.id, attachment=str(f))
    got = {}

    def fake(prompt, files=None):
        got["files"], got["prompt"] = files, prompt
        return json.dumps({"reply": "", "steps": [{"name": "x", "minutes": 60}], "total_minutes": 60})

    pl.make_plan(store, a.id, _profile(), fake, now=_kst("2026-10-29T12:00"))
    assert got["files"] == [str(f)] and "Read 도구로" in got["prompt"]


def test_cli_plan_questions_saved(tmp_path, monkeypatch):
    from manager import cli
    store, cfg = _cli_env(tmp_path)
    a = store.add("미적분 주제탐구", "2026-11-20", all_day=True, category="Assignment", deadline=True)
    n = {"i": 0}

    def fake_call(prompt, ccfg, files=None):
        n["i"] += 1
        if n["i"] == 1:
            return json.dumps({"reply": "", "questions": ["분량은?", "주제는?"], "steps": []})
        assert "분량은? → A4 3장" in prompt and "질문은 하지 마" in prompt
        return json.dumps({"reply": "ok", "complexity": 3, "total_minutes": 60, "steps": [{"name": "x", "minutes": 60}]})

    monkeypatch.setattr(ai, "call_claude", fake_call)
    monkeypatch.setattr(school, "fetch_range", lambda *a, **k: [])  # 테스트에선 학교 사이트 접속 안 함
    answers = iter(["A4 3장", "", "n"])  # 질문 답(두 번째는 건너뜀) → 계획 미리보기 → 취소
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    cli.cmd_plan(store, cfg, a.id)
    assert store.get(a.id).scope == "분량은? → A4 3장" and n["i"] == 2


def test_pack_splits_to_fit_and_keeps_order():
    """평일 여유 90분(210−예비120)인데 100분 조각 → 쪼개서 넣고, 못 넣으면 이후 작업은 배치 안 함."""
    p = _profile()
    slots = pl.free_slots(p, [], _kst("2026-11-02T00:00"), _kst("2026-11-05T00:00"))  # 월~수
    placed, failed = pl.pack([pl.Chunk("학습", 100), pl.Chunk("정리", 60)], slots, p)
    # 월: 학습 70분(그날 한도 90) → 이후 날짜에 나머지, 하루 한도 지키며 순서대로
    assert not failed and placed[0].minutes == 70 and sum(x.minutes for x in placed) == 160
    assert placed[-1].name.startswith("정리") or "정리" in placed[-1].name
    by_day = {}
    for x in placed:
        by_day[x.start.date()] = by_day.get(x.start.date(), 0) + x.minutes
    assert max(by_day.values()) <= 90
    assert all(a.end <= b.start for a, b in zip(placed, placed[1:]))
    one_day = pl.free_slots(p, [], _kst("2026-11-02T00:00"), _kst("2026-11-03T00:00"))
    placed, failed = pl.pack([pl.Chunk("A", 90), pl.Chunk("B", 60), pl.Chunk("C", 30)], one_day, p)
    assert [x.name for x in placed] == ["A"] and [c.name for c in failed] == ["B", "C"]


def test_tidy_names_and_merge():
    p = _profile()
    t = _kst("2026-11-04T19:30")
    raw = [pl.Placed("학습 (1/4) ②", t, t + timedelta(minutes=30)),
           pl.Placed("학습 (2/4) ①", t + timedelta(minutes=40), t + timedelta(minutes=90)),
           pl.Placed("학습 (2/4) ② ①", t + timedelta(days=1), t + timedelta(days=1, minutes=30))]
    out = pl.tidy(raw, p)
    assert [(x.name, x.minutes) for x in out] == [("학습 (1/2)", 80), ("학습 (2/2)", 30)]



# ---------- 학사일정 / 늦은 시간 제한 ----------

SAMPLE_HTML = """
<ul>
 <li><dl><dt>26.11.13</dt><dd class="tch-tit"><div class="tch-tit-wrap">- 재량휴업</div></dd><dd class="tch-ctnt"></dd></dl></li>
 <li><dl><dt>26.11.28</dt><dd class="tch-tit">
   <div class="tch-tit-wrap">
     - 미귀가
   </div></dd><dd class="tch-ctnt"></dd></dl></li>
 <li><dl><dt>26.12.01~26.12.04</dt><dd class="tch-tit"><div class="tch-tit-wrap">- 기말고사</div></dd></dl></li>
</ul>"""


def test_school_parse_and_weekend_rule(tmp_path):
    evs = school.parse_page(SAMPLE_HTML)
    assert evs == [{"start": "2026-11-13", "end": "2026-11-13", "title": "재량휴업"},
                   {"start": "2026-11-28", "end": "2026-11-28", "title": "미귀가"},
                   {"start": "2026-12-01", "end": "2026-12-04", "title": "기말고사"}]
    cache = school.save_cache(school.cache_path(tmp_path), evs)
    stays = school.stay_dates(school.load_cache(school.cache_path(tmp_path)))
    assert stays == {date(2026, 11, 28)} and not school.is_stale(cache)
    # 11/27(금)·28(토) = 미귀가주, 11/20(금)·21(토) = 귀가주, 목요일은 해당 없음
    assert not school.is_home_weekend_day(date(2026, 11, 27), stays)
    assert not school.is_home_weekend_day(date(2026, 11, 28), stays)
    assert school.is_home_weekend_day(date(2026, 11, 20), stays)
    assert school.is_home_weekend_day(date(2026, 11, 21), stays)
    assert not school.is_home_weekend_day(date(2026, 11, 19), stays)


def test_latest_end_except_home_weekend():
    p = pl.load_profile(None)  # latest_end 22:00, 귀가주 금·토 24:00
    p.availability = {k: pl.parse_windows("20:00-24:00") for k in pl.DAY_KEYS}
    p.stay_dates = {date(2026, 11, 28)}

    def end_of(day):
        d = _kst(f"{day}T00:00")
        return pl.free_slots(p, [], d, d + timedelta(days=1))[-1][1].strftime("%H:%M")

    assert end_of("2026-11-19") == "22:00"  # 목: 22시까지
    assert end_of("2026-11-20") == "00:00"  # 귀가주 금: 자정까지
    assert end_of("2026-11-21") == "00:00"  # 귀가주 토
    assert end_of("2026-11-22") == "22:00"  # 일
    assert end_of("2026-11-27") == "22:00"  # 미귀가주 금
    assert end_of("2026-11-28") == "22:00"  # 미귀가주 토



def test_school_parse_real_page_snippet():
    """학교 홈페이지 원본 HTML (2026년 10월, CRLF·탭 그대로)."""
    raw = ('<ul class="tch-sch-lst">\r\n\t<li>\r\n\t\t<dl>\r\n\t\t\t<dt>26.09.29~26.10.02</dt>\r\n\t\t\t'
           '<dd class="tch-tit">\r\n\t\t\t\t<div class="tch-tit-wrap">\r\n\t\t\t\t\t- 중간고사/2학기고사\r\n\t\t\t\t</div>'
           '\r\n\t\t\t</dd>\r\n\t\t\t<dd class="tch-ctnt"></dd>\r\n\t\t</dl>\r\n\t</li>\r\n\t<li>\r\n\t\t<dl>\r\n\t\t\t'
           '<dt>26.10.31</dt>\r\n\t\t\t<dd class="tch-tit">\r\n\t\t\t\t<div class="tch-tit-wrap">\r\n\t\t\t\t\t- 미귀가'
           '\r\n\t\t\t\t</div>\r\n\t\t\t</dd>\r\n\t\t\t<dd class="tch-ctnt"></dd>\r\n\t\t</dl>\r\n\t</li>\r\n</ul>')
    assert school.parse_page(raw) == [
        {"start": "2026-09-29", "end": "2026-10-02", "title": "중간고사/2학기고사"},
        {"start": "2026-10-31", "end": "2026-10-31", "title": "미귀가"}]



def test_submission_fields(store, tmp_path, capsys, monkeypatch):
    e = store.add("KJWP 보고서", "2026-11-09", all_day=True, category="Competition", deadline=True)
    e = store.update(e.id, submit_link="https://kjwp.example.org/submit")
    props = sync_notion.event_properties(e)
    assert "Submission" not in props
    assert props["Submission Link"] == {"url": "https://kjwp.example.org/submit"}
    e2 = store.add("화학 보고서", "2026-11-10", all_day=True)
    p2 = sync_notion.event_properties(store.update(e2.id, submit_link="교무실 앞 제출함"))
    assert p2["Submission Link"] == {"url": "교무실 앞 제출함"}  # 링크가 없으면 방법을 텍스트로
    assert sync_notion.event_properties(store.add("x", "2026-11-11", all_day=True))["Submission Link"] == {"url": None}
    # Obsidian: URL이면 링크, 아니면 글자
    assert "📤[제출](https://kjwp.example.org/submit)" in sync_obsidian._line(store.get(e.id))
    assert "📤교무실 앞 제출함" in sync_obsidian._line(store.get(e2.id))
    # submit 명령: 링크면 브라우저로 열기
    from manager import cli
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
    cli.cmd_submit(store, e.id)
    assert opened == ["https://kjwp.example.org/submit"]
    # AI add 액션 (옛 형식 submit_via만 와도 submit_link로)
    ok, fail = ai.apply_actions(store, [{"op": "add", "title": "발표", "start": "2026-11-12", "all_day": True,
                                         "submit_link": "https://forms.gle/x"},
                                        {"op": "add", "title": "실험 보고서", "start": "2026-11-13", "all_day": True,
                                         "submit_via": "드롭박스"}])
    assert not fail and [x.submit_link for x in store.all()[-2:]] == ["https://forms.gle/x", "드롭박스"]


def test_submit_via_migrates_to_link(tmp_path):
    from manager.db import Store
    p = tmp_path / "m.db"
    s = Store(p)
    e1 = s.add("a", "2026-11-01", all_day=True)
    e2 = s.add("b", "2026-11-02", all_day=True)
    s.conn.execute("UPDATE events SET submit_via='드롭박스' WHERE id=?", (e1.id,))
    s.conn.execute("UPDATE events SET submit_via='드롭박스', submit_link='https://x.kr' WHERE id=?", (e2.id,))
    s.conn.commit(); s.conn.close()
    s = Store(p)
    assert (s.get(e1.id).submit_link, s.get(e2.id).submit_link) == ("드롭박스", "https://x.kr")
    assert not s.get(e1.id).submit_via



def test_clean_title():
    from manager.db import clean_title as c
    assert c("캔위성 계획서 제출") == "캔위성 계획서"
    assert c("대수 보고서 제출 마감") == "대수 보고서"
    assert c("화학 독서감상문 (마감)") == "화학 독서감상문"
    assert c("대수 보고서 11월 16일 21시까지 제출") == "대수 보고서"
    assert c("보고서 11/16까지") == "보고서"
    assert c("제출물 정리") == "제출물 정리"   # 다른 단어의 일부면 유지
    assert c("마감") == "마감"                # 다 지워지면 원래대로


def test_team_members(store, tmp_path, capsys):
    e = store.add("캔위성 계획서 제출", "2026-11-07T23:59", deadline=True, category="Assignment", team=["이강우"])
    assert e.title == "캔위성 계획서" and e.team_list == ["이강우"]
    e = store.update(e.id, team="이강우, 김헨리", title="캔위성 계획서 마감")
    assert e.title == "캔위성 계획서" and e.team_list == ["이강우", "김헨리"]
    assert sync_notion.event_properties(e)["Team"] == {"multi_select": [{"name": "이강우"}, {"name": "김헨리"}]}
    assert "👥이강우, 김헨리" in sync_obsidian._line(e)
    assert store.known_members() == ["김헨리", "이강우"]
    ok, fail = ai.apply_actions(store, [{"op": "add", "title": "FTC 회의", "start": "2026-11-10T19:00",
                                         "team": ["오건령", "김준식"]}])
    assert not fail and store.all()[-1].team == "오건령, 김준식"
    from manager.cli import main
    cfgp = _write_cfg(tmp_path)
    assert main(["--config", str(cfgp), "add", "KJWP 회의", "2026-11-11 19:00", "-m", "김헨리", "-m", "오건령, 김준식"]) == 0
    assert main(["--config", str(cfgp), "edit", "1", "-m", "-"]) == 0
    out = capsys.readouterr().out
    assert "👥김헨리, 오건령, 김준식" in out


# ---------------- Notion → 로컬 (pull) ----------------
class PullNotion:
    """DB 쿼리 결과를 직접 정해주는 가짜 Notion."""
    def __init__(self, pages, outside=None):
        self.pages = pages              # DB에 있는 페이지 목록
        self.outside = outside or {}    # DB 밖 페이지 id → GET 응답 (없으면 404)

    def request(self, method, path, body=None, retries=3):
        if path.endswith("/query"):
            return {"results": self.pages, "has_more": False}
        if method == "GET" and path.startswith("/pages/"):
            pid = path.split("/")[-1]
            if pid in self.outside:
                return self.outside[pid]
            raise sync_notion.NotionError(f"GET {path} → HTTP 404: not found")
        raise AssertionError((method, path))


def _page(pid, title, start=None, end=None, done=False, cat=None, teachers=(), team=(), link=None):
    d = lambda v: {"date": {"start": v, "end": None, "time_zone": None} if v else None}
    return {"id": pid, "properties": {
        "Name": {"title": [{"plain_text": title}]}, "Start Date": d(start), "End Date": d(end),
        "Done": {"checkbox": done}, "Category": {"select": {"name": cat} if cat else None},
        "Teacher": {"multi_select": [{"name": t} for t in teachers]},
        "Team": {"multi_select": [{"name": t} for t in team]}, "Submission Link": {"url": link}}}


def _linked(store, title, pid, **kw):
    ev = store.add(title, kw.pop("start"), kw.pop("end", None), all_day=kw.pop("all_day", True), **kw)
    store.mark_notion(ev.id, pid)
    return store.get(ev.id)


def test_pull_applies_notion_edits(store):
    cfg = NotionConfig(enabled=True, token="t", database_id="db")
    a = _linked(store, "화학 독서감상문", "p-a", start="2026-09-19", deadline=True, category="Assignment")
    b = _linked(store, "캔위성 계획서", "p-b", start="2026-11-07T23:59+09:00", all_day=False, deadline=True)
    c = _linked(store, "KJWP", "p-c", start="2026-06-02", end="2026-11-09", category="Competition")
    pages = [
        _page("p-a", "화학 독서감상문", end="2026-09-19", done=True, cat="Assignment"),       # 완료 체크
        _page("p-b", "캔위성 계획서", end="2026-11-07T14:59:00.000Z", link="드롭박스"),      # 같은 시각(UTC), 링크만 바뀜
        _page("p-c", "KJWP 본선", start="2026-06-02", end="2026-11-20", cat="Competition", team=["김헨리"]),
    ]
    s = sync_notion.pull(store, cfg, PullNotion(pages))
    assert not s["conflicts"] and not s["skipped"]
    got = {i: ch for i, _, ch in s["updated"]}
    assert got[a.id] == {"status": "done"}
    assert got[b.id] == {"submit_link": "드롭박스"}          # 시간은 같은 시각이라 안 바뀜
    assert got[c.id] == {"title": "KJWP 본선", "team": "김헨리", "end": "2026-11-20"}
    assert store.get(c.id).end == "2026-11-20" and store.get(a.id).status == "done"
    assert not store.notion_pending()                         # 반영 후엔 다시 올릴 게 없음
    # 두 번째 pull은 변화 없음
    assert not any(sync_notion.pull(store, cfg, PullNotion(pages)).values())


def test_pull_conflict_keeps_local(store):
    cfg = NotionConfig(enabled=True, token="t", database_id="db")
    a = _linked(store, "보고서", "p-a", start="2026-11-10")
    store.update(a.id, title="보고서 최종")  # 로컬도 바뀜 (아직 안 올림)
    s = sync_notion.pull(store, cfg, PullNotion([_page("p-a", "보고서 초안", start="2026-11-10")]))
    assert s["conflicts"] and store.get(a.id).title == "보고서 최종"


def test_pull_creates_and_deletes(store):
    cfg = NotionConfig(enabled=True, token="t", database_id="db")
    moved = _linked(store, "짭커톤", "p-moved", start="2026-05-20")
    trashed = _linked(store, "옛 일정", "p-trash", start="2026-05-21")
    keep = _linked(store, "남는 일정", "p-keep", start="2026-05-22")
    client = PullNotion(
        [_page("p-keep", "남는 일정", start="2026-05-22"),
         _page("p-new", "물리 보고서 제출", end="2026-11-30", cat="Assignment", link="https://x.kr"),
         _page("p-nodate", "날짜 없는 메모")],
        outside={"p-moved": {"id": "p-moved", "archived": False, "parent": {"type": "page_id", "page_id": "comp"}}})
    s = sync_notion.pull(store, cfg, client)
    assert {i for i, _ in s["deleted"]} == {moved.id, trashed.id}
    assert store.get(moved.id) is None and store.get(trashed.id) is None and store.get(keep.id)
    # 사라진 페이지는 연결이 끊겨서 push 때 archive하지 않음
    assert not [e for e in store.notion_pending() if e.deleted]
    new = store.get(s["created"][0][0])
    assert (new.title, new.deadline, new.start, new.submit_link) == ("물리 보고서", True, "2026-11-30", "https://x.kr")
    assert new.notion_page_id == "p-new"
    assert store.notion_pending() == [store.get(new.id)]  # 제목이 정리돼서 Notion도 고치도록 남김
    assert s["skipped"][0][1] == "날짜 없는 메모"


def test_pull_dry_run_changes_nothing(store):
    cfg = NotionConfig(enabled=True, token="t", database_id="db")
    a = _linked(store, "보고서", "p-a", start="2026-11-10")
    s = sync_notion.pull(store, cfg, PullNotion([_page("p-a", "보고서", start="2026-11-12")]), dry_run=True)
    assert s["updated"] and store.get(a.id).start == "2026-11-10"


def _pull_env(store, monkeypatch, pages, outside=None):
    from manager import cli
    from manager.config import Config
    cfg = Config(db_path=Path(":memory:"), auto_sync=False,
                 notion=NotionConfig(enabled=True, token="t", database_id="db"))
    real = sync_notion.plan_pull
    monkeypatch.setattr(sync_notion, "plan_pull", lambda st, nc, client=None: real(st, nc, PullNotion(pages, outside)))
    return cli, cfg


def test_interactive_pull_drop_then_apply(store, monkeypatch, capsys):
    a = _linked(store, "보고서", "p-a", start="2026-11-10")
    b = _linked(store, "발표", "p-b", start="2026-11-11")
    cli, cfg = _pull_env(store, monkeypatch, [_page("p-a", "보고서", start="2026-11-12"),
                                               _page("p-b", "발표", start="2026-11-11", done=True)])
    answers = iter(["1번 빼", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert cli.interactive_pull(store, cfg)
    out = capsys.readouterr().out
    assert out.count("Notion에서 가져올 내용") == 2           # 수정 후 미리보기 다시 보여줌
    assert store.get(a.id).start == "2026-11-10"              # 뺀 항목은 적용 안 함
    assert store.get(b.id).status == "done"


def test_interactive_pull_cancel(store, monkeypatch):
    a = _linked(store, "보고서", "p-a", start="2026-11-10")
    cli, cfg = _pull_env(store, monkeypatch, [_page("p-a", "보고서 v2", start="2026-11-10")])
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert not cli.interactive_pull(store, cfg)
    assert store.get(a.id).title == "보고서"


def test_interactive_pull_ai_revision(store, monkeypatch, capsys):
    a = _linked(store, "보고서", "p-a", start="2026-11-10")
    cli, cfg = _pull_env(store, monkeypatch, [_page("p-a", "보고서", start="2026-11-12"),
                                               _page("p-new", "KSEF")])
    prompts = []

    def fake_claude(prompt, c, read_files=None):
        prompts.append(prompt)
        return json.dumps({"reply": "KSEF는 12/1 마감으로 추가하고 1번 날짜는 11/13으로 바꿨어.", "items": [
            {"no": 1, "kind": "update", "id": a.id, "page_id": "p-a", "title": "보고서",
             "changes": {"start": "2026-11-13", "evil": 1}},
            {"no": 2, "kind": "create", "page_id": "p-new", "title": "KSEF",
             "fields": {"title": "KSEF", "start": "2026-12-01", "end": None, "deadline": True, "all_day": True}},
            {"no": 99, "kind": "delete", "id": 1}]})  # 없는 항목은 무시
    monkeypatch.setattr(ai, "call_claude", fake_claude)
    answers = iter(["KSEF는 12월 1일 마감으로 넣고 1번은 13일로", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert cli.interactive_pull(store, cfg)
    assert "사용자 요청: KSEF는" in prompts[0]
    assert store.get(a.id).start == "2026-11-13"
    new = [e for e in store.all() if e.title == "KSEF"][0]
    assert (new.start, new.deadline, new.notion_page_id) == ("2026-12-01", True, "p-new")
    assert "· 건너뜀 KSEF" in capsys.readouterr().out          # 처음 미리보기에선 날짜 없어서 건너뜀


def test_sanitize_pull_items_blocks_bad_kinds():
    orig = [{"no": 1, "kind": "delete", "id": 5, "page_id": "p", "title": "x"},
            {"no": 2, "kind": "skip", "id": 6, "page_id": "q", "title": "y", "reason": "확인 실패"}]
    out = ai.sanitize_pull_items([{"no": 1, "kind": "update"}, {"no": 2, "kind": "create", "fields": {"start": "2026-01-01"}}], orig)
    assert [o["kind"] for o in out] == ["delete", "skip"]


def test_teacher_learn_from_events(store, monkeypatch, capsys):
    from manager import cli
    from manager.config import Config
    store.add("미적분 주제탐구 수행평가", "2026-11-06", all_day=True, category="Assignment", teachers=["연광흠 선생님"])
    store.add("지구시스템 탐구 프로젝트", "2026-11-12", all_day=True, category="Assignment", teachers=["이설아 선생님"])
    store.add("지구시스템 질문 노트", "2026-11-19", all_day=True, category="Assignment", teachers=["이설아 선생님"])
    store.add("화학전지 논문 찾기", "2026-11-14", all_day=True, category="Assignment", teachers=["송현주 선생님"])
    store.add("캔위성", "2026-05-07", all_day=True, category="Competition", teachers=["이누리 선생님"])  # 과제 아님
    store.add("화학 독서감상문", "2026-09-19", all_day=True, category="Assignment")  # 선생님 비어 있음 → 등록 후 채워짐
    g = {x["subject"]: x["teacher"] for x in store.guess_subject_teachers()}
    assert g == {"미적분": "연광흠 선생님", "지구시스템": "이설아 선생님", "화학": "송현주 선생님"}
    cfg = Config(db_path=Path(":memory:"), auto_sync=False)
    answers = iter(["1번 빼", "2번 지구시스템과학", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    cli.cmd_teacher_learn(store, cfg)
    assert dict(store.subject_teachers()) == {"지구시스템과학": "이설아 선생님", "화학": "송현주 선생님"}
    assert [e for e in store.all() if e.title == "화학 독서감상문"][0].teachers == "송현주 선생님"
    assert not [x for x in store.guess_subject_teachers() if x["subject"] == "화학"]  # 이미 등록됨


def test_deadline_start_is_entered_date_and_pull_keeps_deadline(store):
    cfg = NotionConfig(enabled=True, token="t", database_id="db")
    a = _linked(store, "수행평가", "p-a", start="2026-11-12", deadline=True)
    assert a.entered == date.today().isoformat()
    # Notion에서 Start(입력일)만 바꿈 → 마감 일정은 그대로, 입력일만 수정
    s = sync_notion.pull(store, cfg, PullNotion([_page("p-a", "수행평가", start="2026-10-20", end="2026-11-12")]))
    assert s["updated"][0][2] == {"entered": "2026-10-20"}
    e = store.get(a.id)
    assert (e.start, e.end, e.deadline, e.entered) == ("2026-11-12", None, True, "2026-10-20")


def test_entered_backfilled_on_old_db(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    s = Store(p)
    s.add("옛 과제", "2026-11-01", all_day=True, deadline=True)
    s.conn.execute("UPDATE events SET entered = ''"); s.conn.commit(); s.conn.close()
    assert Store(p).all()[0].entered == date.today().isoformat()


def test_deadline_start_only_today_when_missing(store):
    no_start = store.add("보고서", "2026-11-12", all_day=True, deadline=True)
    with_start = store.add("탐구", "2026-11-01", "2026-11-20", all_day=True, deadline=True)
    p1, p2 = sync_notion.event_properties(no_start), sync_notion.event_properties(with_start)
    assert (p1["Start Date"]["date"]["start"], p1["End Date"]["date"]["start"]) == (date.today().isoformat(), "2026-11-12")
    assert (p2["Start Date"]["date"]["start"], p2["End Date"]["date"]["start"]) == ("2026-11-01", "2026-11-20")
