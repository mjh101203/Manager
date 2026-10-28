"""SQLite 저장소 — 일정의 원본(source of truth).

시간 형식:
  - 종일 일정: start/end = "YYYY-MM-DD"
  - 시간 일정: start/end = ISO 8601 + 오프셋, 예) "2026-10-28T15:00:00+09:00"
문자열 비교로 정렬/범위 검색이 되도록 항상 이 형식으로 정규화해서 저장한다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    start            TEXT NOT NULL,
    end              TEXT,
    all_day          INTEGER NOT NULL DEFAULT 0,
    deadline         INTEGER NOT NULL DEFAULT 0,      -- 마감형 일정 (날짜 = 마감일)
    category         TEXT NOT NULL DEFAULT '',
    location         TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'todo',   -- todo | done | cancelled
    deleted          INTEGER NOT NULL DEFAULT 0,      -- soft delete (백업 쪽 삭제 반영용)
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    notion_page_id   TEXT,
    notion_synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE TABLE IF NOT EXISTS categories (      -- 태그(= Notion Category) 목록
    name       TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tag_ops (         -- Notion에 아직 반영 안 된 태그 이름 변경/삭제
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    old        TEXT NOT NULL,
    new        TEXT NOT NULL DEFAULT '',     -- '' = 태그 없음으로
    created_at TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0
);
"""

EDITABLE = ("title", "start", "end", "all_day", "deadline", "category", "location", "notes", "status")
STATUSES = ("todo", "done", "cancelled")


def now_iso() -> str:
    # 마이크로초까지 — 같은 초 안의 수정/동기화 순서도 구분되게
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def normalize_time(value: str | None, all_day: bool) -> str | None:
    """여러 입력 형식을 저장 형식으로 맞춤. 오프셋 없으면 로컬 시간대로 간주."""
    if value is None or value == "":
        return None
    value = value.strip()
    if "T" not in value and " " in value:
        value = value.replace(" ", "T", 1)
    if all_day:
        return date.fromisoformat(value[:10]).isoformat()
    if len(value) == 10:  # 날짜만 들어왔는데 시간 일정 → 00:00
        value += "T00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 로컬 tz 부여
    return dt.isoformat(timespec="seconds")


@dataclass
class Event:
    id: int
    title: str
    start: str
    end: str | None
    all_day: bool
    deadline: bool
    category: str
    location: str
    notes: str
    status: str
    deleted: bool
    created_at: str
    updated_at: str
    notion_page_id: str | None
    notion_synced_at: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Event":
        d = dict(r)
        d["all_day"] = bool(d["all_day"])
        d["deadline"] = bool(d.get("deadline", 0))
        d["deleted"] = bool(d["deleted"])
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def day(self) -> str:
        return self.start[:10]

    def time_label(self) -> str:
        if self.all_day:
            return "마감" if self.deadline and not self.end else "종일"
        s = self.start[11:16]
        if self.end and self.end[:10] == self.start[:10]:
            return f"{s}–{self.end[11:16]}"
        if self.end:
            return f"{s}–{self.end[5:10]} {self.end[11:16]}"
        return s


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "deadline" not in cols:  # 구버전 DB 마이그레이션
            self.conn.execute("ALTER TABLE events ADD COLUMN deadline INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        # 일정에 이미 쓰인 카테고리는 태그 목록에도 등록
        self.conn.execute("INSERT OR IGNORE INTO categories (name, created_at) "
                          "SELECT DISTINCT category, ? FROM events WHERE category != '' AND deleted = 0", (now_iso(),))
        self.conn.commit()

    # ---------- 조회 ----------
    def get(self, event_id: int, include_deleted: bool = False) -> Event | None:
        q = "SELECT * FROM events WHERE id = ?" + ("" if include_deleted else " AND deleted = 0")
        r = self.conn.execute(q, (event_id,)).fetchone()
        return Event.from_row(r) if r else None

    def range(self, start_day: str | None = None, end_day: str | None = None,
              include_done: bool = True) -> list[Event]:
        """start_day 이상, end_day '미만' (YYYY-MM-DD)."""
        q = "SELECT * FROM events WHERE deleted = 0"
        args: list = []
        if start_day:
            q += " AND COALESCE(end, start) >= ?"
            args.append(start_day)
        if end_day:
            q += " AND start < ?"
            args.append(end_day)
        if not include_done:
            q += " AND status = 'todo'"
        q += " ORDER BY start, id"
        return [Event.from_row(r) for r in self.conn.execute(q, args)]

    def all(self, include_deleted: bool = False) -> list[Event]:
        q = "SELECT * FROM events" + ("" if include_deleted else " WHERE deleted = 0") + " ORDER BY start, id"
        return [Event.from_row(r) for r in self.conn.execute(q)]

    def categories(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT name FROM categories ORDER BY name")]

    # ---------- 태그(카테고리) ----------
    def tags(self) -> list[tuple[str, int]]:
        """(태그, 남은 일정 수) — 일정 없는 태그도 포함."""
        q = ("SELECT c.name, COUNT(e.id) FROM categories c LEFT JOIN events e"
             " ON e.category = c.name AND e.deleted = 0 GROUP BY c.name ORDER BY c.name")
        return [(r[0], r[1]) for r in self.conn.execute(q)]

    def has_tag(self, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM categories WHERE name = ?", (name,)).fetchone() is not None

    def add_tag(self, name: str) -> bool:
        """새로 만들었으면 True, 이미 있으면 False."""
        name = name.strip()
        if not name:
            raise ValueError("태그 이름이 비어 있음")
        cur = self.conn.execute("INSERT OR IGNORE INTO categories (name, created_at) VALUES (?, ?)", (name, now_iso()))
        self.conn.commit()
        return cur.rowcount > 0

    def rename_tag(self, old: str, new: str) -> int:
        """태그 이름 변경 — 해당 일정도 전부 바뀜(다음 동기화 때 Notion 반영). 바뀐 일정 수 반환."""
        new = new.strip()
        if not self.has_tag(old):
            raise KeyError(f"'{old}' 태그 없음")
        if not new:
            raise ValueError("새 이름이 비어 있음")
        self.add_tag(new)
        cur = self.conn.execute("UPDATE events SET category = ?, updated_at = ? WHERE category = ? AND deleted = 0",
                                (new, now_iso(), old))
        if new != old:
            self.conn.execute("DELETE FROM categories WHERE name = ?", (old,))
            self._record_tag_op(old, new)
        self.conn.commit()
        return cur.rowcount

    def remove_tag(self, name: str, move_to: str = "") -> int:
        """태그 삭제. 해당 일정은 move_to 태그로 옮기거나(기본) 태그 없음으로. 바뀐 일정 수 반환."""
        if not self.has_tag(name):
            raise KeyError(f"'{name}' 태그 없음")
        if move_to:
            self.add_tag(move_to)
        cur = self.conn.execute("UPDATE events SET category = ?, updated_at = ? WHERE category = ? AND deleted = 0",
                                (move_to, now_iso(), name))
        self.conn.execute("DELETE FROM categories WHERE name = ?", (name,))
        self._record_tag_op(name, move_to)
        self.conn.commit()
        return cur.rowcount

    def _record_tag_op(self, old: str, new: str) -> None:
        self.conn.execute("INSERT INTO tag_ops (old, new, created_at) VALUES (?, ?, ?)", (old, new, now_iso()))

    def pending_tag_ops(self) -> list[tuple[int, str, str]]:
        """(id, old, new) — 만든 순서대로. new == '' 이면 태그 제거."""
        return [(r[0], r[1], r[2]) for r in
                self.conn.execute("SELECT id, old, new FROM tag_ops WHERE done = 0 ORDER BY id")]

    def mark_tag_op_done(self, op_id: int) -> None:
        self.conn.execute("UPDATE tag_ops SET done = 1 WHERE id = ?", (op_id,))
        self.conn.commit()

    # ---------- 수정 ----------
    def add(self, title: str, start: str, end: str | None = None, all_day: bool = False,
            category: str = "", location: str = "", notes: str = "", deadline: bool = False) -> Event:
        if not title.strip():
            raise ValueError("제목이 비어 있음")
        s = normalize_time(start, all_day)
        e = normalize_time(end, all_day)
        if s is None:
            raise ValueError("시작 시간이 필요함")
        if e and e < s:
            raise ValueError(f"종료({e})가 시작({s})보다 빠름")
        if category:
            self.add_tag(category)
        ts = now_iso()
        cur = self.conn.execute(
            "INSERT INTO events (title, start, end, all_day, deadline, category, location, notes, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (title.strip(), s, e, int(all_day), int(deadline), category or "", location or "", notes or "", ts, ts),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)  # type: ignore[arg-type]

    def update(self, event_id: int, **fields) -> Event:
        ev = self.get(event_id)
        if not ev:
            raise KeyError(f"#{event_id} 일정 없음")
        unknown = set(fields) - set(EDITABLE)
        if unknown:
            raise ValueError(f"수정 불가 필드: {', '.join(sorted(unknown))}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"status는 {STATUSES} 중 하나")
        all_day = bool(fields.get("all_day", ev.all_day))
        if "deadline" in fields:
            fields["deadline"] = int(bool(fields["deadline"]))
        if fields.get("category"):
            self.add_tag(fields["category"])
        if "all_day" in fields:
            fields["all_day"] = int(all_day)
            # 종일 여부가 바뀌면 시간 형식도 다시 맞춰야 함
            fields.setdefault("start", ev.start)
            fields.setdefault("end", ev.end)
        if "start" in fields:
            fields["start"] = normalize_time(fields["start"], all_day)
        if "end" in fields:
            fields["end"] = normalize_time(fields["end"], all_day)
        s = fields.get("start", ev.start)
        e = fields.get("end", ev.end)
        if e and e < s:
            raise ValueError(f"종료({e})가 시작({s})보다 빠름")
        if not fields:
            return ev
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE events SET {cols} WHERE id = ?", (*fields.values(), event_id))
        self.conn.commit()
        return self.get(event_id)  # type: ignore[return-value]

    def delete(self, event_id: int) -> Event:
        ev = self.get(event_id)
        if not ev:
            raise KeyError(f"#{event_id} 일정 없음")
        self.conn.execute("UPDATE events SET deleted = 1, updated_at = ? WHERE id = ?", (now_iso(), event_id))
        self.conn.commit()
        return ev

    # ---------- 동기화 상태 ----------
    def notion_pending(self) -> list[Event]:
        """Notion에 반영 안 된 변경분 (삭제 포함)."""
        q = ("SELECT * FROM events WHERE"
             " (deleted = 0 AND (notion_page_id IS NULL OR notion_synced_at IS NULL OR updated_at > notion_synced_at))"
             " OR (deleted = 1 AND notion_page_id IS NOT NULL)")
        return [Event.from_row(r) for r in self.conn.execute(q)]

    def mark_notion(self, event_id: int, page_id: str | None) -> None:
        self.conn.execute("UPDATE events SET notion_page_id = ?, notion_synced_at = ? WHERE id = ?",
                          (page_id, now_iso(), event_id))
        self.conn.commit()
