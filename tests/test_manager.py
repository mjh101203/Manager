import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from manager import ai, sync_notion, sync_obsidian
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
    assert set(props) == {"Name", "Start Date", "End Date", "Done", "Category"}  # Notion 전용 칸은 건드리지 않음
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
    assert props["Start Date"]["date"] is None
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
    """n → 수정 요청 → 새 제안 → Y 로 적용. AI에는 직전 제안과 수정 요청이 전달돼야 함."""
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

    answers = iter(["n", "2번 빼줘", ""])
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

    answers = iter(["시간 8시로", "n", ""])  # 바로 수정 입력 → 다시 n → 엔터(취소)
    monkeypatch.setattr(ai, "interpret", fake_interpret)
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    cli.handle_message(store, cfg, "x 추가", [])
    assert seen == ["", "시간 8시로"] and store.all() == []


def test_edit_interactive_and_flags(tmp_path, monkeypatch):
    from manager import cli
    store, cfg = _cli_env(tmp_path)
    store.add("보고서", "2026-11-16", all_day=True, category="Assignment")
    # 제목·태그·deadline만 바꾸고 나머지는 엔터
    answers = iter(["대수 보고서", "", "", "Research", "", "", "y", "", "y"])  # 마지막 y = 새 태그 생성 확인
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    cli.cmd_edit(store, cfg, 1, "", {})
    e = store.get(1)
    assert (e.title, e.category, e.deadline) == ("대수 보고서", "Research", True)
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
