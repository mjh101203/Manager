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
    teachers         TEXT NOT NULL DEFAULT '',     -- 담당 선생님, 쉼표 구분 (Notion Teacher)
    team             TEXT NOT NULL DEFAULT '',     -- 팀원, 쉼표 구분 (Notion Team)
    subject          TEXT NOT NULL DEFAULT '',     -- 과목 (Assignment 선생님 자동 입력용)
    parent_id        INTEGER,                      -- 작업 세션이면 원래 과제 id
    est_minutes      INTEGER,                      -- 예상 소요(분): 과제=총합, 세션=그 세션 길이
    actual_minutes   INTEGER,                      -- 실제 소요(분) — 완료 때 기록, 추정 보정에 사용
    task_type        TEXT NOT NULL DEFAULT '',     -- 과제 유형 (보고서/발표/문제풀이…)
    complexity       INTEGER,                      -- 난이도 1~5
    topic            TEXT NOT NULL DEFAULT '',     -- 과제 주제 (예: 해밍 코드)
    scope            TEXT NOT NULL DEFAULT '',     -- 범위·요구사항 (분량, 형식, 산출물)
    attachment       TEXT NOT NULL DEFAULT '',     -- 과제 안내문 파일 경로 (PDF·이미지·txt)
    components       TEXT NOT NULL DEFAULT '',     -- 계획 때 AI가 나눈 작업 요소별 예상(JSON) — 보정용
    submit_via       TEXT NOT NULL DEFAULT '',     -- (안 씀: submit_link 하나로 통합)
    submit_link      TEXT NOT NULL DEFAULT '',
    entered          TEXT NOT NULL DEFAULT '',     -- 입력한 날짜 (마감 일정의 Notion Start Date)     -- 제출: 링크(URL) 또는 방법 텍스트 (예: 드롭박스, 교무실 제출함)
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
CREATE TABLE IF NOT EXISTS subject_teachers ( -- 과목 → 선생님 (Assignment 자동 입력)
    subject    TEXT PRIMARY KEY,
    teacher    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tag_ops (         -- Notion에 아직 반영 안 된 태그 이름 변경/삭제
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    old        TEXT NOT NULL,
    new        TEXT NOT NULL DEFAULT '',     -- '' = 태그 없음으로
    created_at TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0
);
"""

# 제목에서 과목을 찾을 때 쓰는 과목명 (긴 이름 우선)
SUBJECT_WORDS = ("국어", "문학", "독서", "화법", "작문", "언어와 매체", "영어", "수학", "대수", "미적분", "기하",
                 "확률과 통계", "확통", "물리", "화학", "생명과학", "생명", "생물", "지구과학", "지구시스템", "정보",
                 "프로그래밍", "인공지능", "한국사", "사회", "윤리", "지리", "역사", "경제", "음악", "미술", "체육",
                 "기술", "가정", "일본어", "중국어", "독일어", "프랑스어", "천문", "융합과학", "과학탐구")
AUTO_TEACHER_CATEGORY = "Assignment"  # 이 태그의 일정에만 과목별 선생님 자동 입력
EDITABLE = ("title", "start", "end", "all_day", "deadline", "teachers", "team", "subject",
            "parent_id", "est_minutes", "actual_minutes", "task_type", "complexity",
            "topic", "scope", "attachment", "components", "submit_link", "entered", "category", "location", "notes", "status")
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


# 제목에서 뺄 단어 — 마감·제출 정보는 deadline / submit_* 칸에 따로 저장하므로 제목엔 넣지 않음
TITLE_NOISE = ("제출 마감", "제출마감", "마감일", "마감", "제출일", "제출하기", "제출", "기한", "까지")


def clean_title(title: str) -> str:
    """'캔위성 계획서 제출' → '캔위성 계획서'. 띄어쓰기로 분리된 단어(또는 괄호 안)일 때만 지움.
    '제출물 정리'처럼 다른 단어의 일부면 그대로 둠. 다 지워지면 원래 제목 유지."""
    import re
    t = (title or "").strip()
    noise = "|".join(re.escape(w) for w in TITLE_NOISE)
    out = re.sub(rf"\(\s*(?:{noise})\s*\)|\[\s*(?:{noise})\s*\]", " ", t)
    out = re.sub(rf"(?:(?<=\s)|^)(?:{noise})(?=\s|$|[,·/])", " ", out)
    # "11/16까지", "11월 16일 21시까지" 같은 기한 표현도 제목에서 뺌 (날짜는 일정 칸에 있음)
    out = re.sub(r"(?:(?<=\s)|^)\d{1,2}\s*(?:/|\.|월)\s*\d{1,2}\s*일?(?:\s*\d{1,2}\s*(?::\d{2}|시))?\s*까지(?=\s|$)",
                 " ", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,·/-")
    return out or t


def split_names(v) -> list[str]:
    """'a, b' / ['a','b'] → ['a','b'] (중복·빈값 제거, 순서 유지)."""
    items = v if isinstance(v, (list, tuple)) else str(v or "").split(",")
    out: list[str] = []
    for x in items:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


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
    teachers: str
    subject: str
    status: str
    deleted: bool
    created_at: str
    updated_at: str
    notion_page_id: str | None
    notion_synced_at: str | None
    parent_id: int | None = None
    est_minutes: int | None = None
    actual_minutes: int | None = None
    task_type: str = ""
    complexity: int | None = None
    topic: str = ""
    scope: str = ""
    attachment: str = ""
    components: str = ""
    submit_via: str = ""
    submit_link: str = ""
    entered: str = ""
    team: str = ""

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Event":
        d = dict(r)
        d["all_day"] = bool(d["all_day"])
        d["deadline"] = bool(d.get("deadline", 0))
        d.setdefault("teachers", "")
        d.setdefault("subject", "")
        for k in ("task_type", "topic", "scope", "attachment", "components", "submit_via", "submit_link", "team", "entered"):
            d[k] = d.get(k) or ""
        d["deleted"] = bool(d["deleted"])
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def teacher_list(self) -> list[str]:
        return split_names(self.teachers)

    @property
    def team_list(self) -> list[str]:
        return split_names(self.team)

    @property
    def day(self) -> str:
        return self.start[:10]

    def time_label(self) -> str:
        if self.all_day:
            if self.deadline:
                return "마감" if not self.end else f"~{self.end[5:10]} 마감"
            return "종일"
        s = self.start[11:16]
        if self.deadline:
            return f"{s} 마감" if not self.end else f"~{self.end[5:10]} {self.end[11:16]} 마감"
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
        for col, ddl in (("deadline", "INTEGER NOT NULL DEFAULT 0"), ("teachers", "TEXT NOT NULL DEFAULT ''"),
                         ("subject", "TEXT NOT NULL DEFAULT ''"), ("parent_id", "INTEGER"),
                         ("est_minutes", "INTEGER"), ("actual_minutes", "INTEGER"),
                         ("task_type", "TEXT NOT NULL DEFAULT ''"), ("complexity", "INTEGER"),
                         ("topic", "TEXT NOT NULL DEFAULT ''"), ("scope", "TEXT NOT NULL DEFAULT ''"),
                         ("attachment", "TEXT NOT NULL DEFAULT ''"), ("components", "TEXT NOT NULL DEFAULT ''"),
                         ("submit_via", "TEXT NOT NULL DEFAULT ''"), ("submit_link", "TEXT NOT NULL DEFAULT ''"),
                         ("team", "TEXT NOT NULL DEFAULT ''"), ("entered", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:  # 구버전 DB 마이그레이션
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
        # 입력일이 없는 기존 일정은 오늘로
        self.conn.execute("UPDATE events SET entered = ? WHERE entered = ''", (date.today().isoformat(),))
        # 제출 방식(submit_via)은 submit_link 하나로 통합: 링크가 없으면 방식 텍스트를 옮김
        self.conn.execute("UPDATE events SET submit_link = submit_via WHERE submit_link = '' AND submit_via != ''")
        self.conn.execute("UPDATE events SET submit_via = '' WHERE submit_via != ''")
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

    # ---------- 과목별 선생님 ----------
    def subject_teachers(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.conn.execute("SELECT subject, teacher FROM subject_teachers ORDER BY subject")]

    def set_subject_teacher(self, subject: str, teacher: str) -> int:
        """과목 선생님 등록/변경. 선생님이 비어 있는 그 과목 Assignment 일정에도 채워 넣고, 채운 일정 수 반환."""
        subject, teacher = subject.strip(), ", ".join(split_names(teacher))
        if not subject or not teacher:
            raise ValueError("과목과 선생님 이름이 필요함")
        self.conn.execute("INSERT INTO subject_teachers (subject, teacher) VALUES (?, ?) "
                          "ON CONFLICT(subject) DO UPDATE SET teacher = excluded.teacher", (subject, teacher))
        self.conn.commit()
        return self.fill_teachers()

    def known_members(self) -> list[str]:
        """일정에 나온 팀원 이름 전부."""
        names: list[str] = []
        for (t,) in self.conn.execute("SELECT team FROM events WHERE deleted = 0 AND team != ''"):
            names += split_names(t)
        return sorted(set(names))

    def known_teachers(self) -> list[str]:
        """일정·과목표에 나온 선생님 이름 전부."""
        names: list[str] = []
        for (t,) in self.conn.execute("SELECT teachers FROM events WHERE deleted = 0 AND teachers != ''"):
            names += split_names(t)
        for _, t in self.subject_teachers():
            names += split_names(t)
        return sorted(set(names))

    def remove_subject_teacher(self, subject: str) -> None:
        cur = self.conn.execute("DELETE FROM subject_teachers WHERE subject = ?", (subject,))
        self.conn.commit()
        if not cur.rowcount:
            raise KeyError(f"'{subject}' 과목 없음")

    def match_subject(self, title: str, subject: str = "") -> str:
        """등록된 과목 중 일치하는 것. subject가 등록돼 있으면 그대로, 아니면 제목에 들어간 가장 긴 과목명."""
        names = [s for s, _ in self.subject_teachers()]
        if subject and subject in names:
            return subject
        hits = [n for n in names if n in (title or "") or (subject and n in subject)]
        return max(hits, key=len) if hits else ""

    def guess_subject_teachers(self) -> list[dict]:
        """이미 선생님이 적힌 Assignment 일정에서 과목→선생님을 추정.
        [{"subject", "teacher", "events": [제목], "others": {다른 선생님: 횟수}, "current": 등록된 값}]
        이미 같은 값으로 등록된 과목은 뺌."""
        from collections import Counter
        words = sorted(set(SUBJECT_WORDS) | {s for s, _ in self.subject_teachers()}, key=len, reverse=True)
        found: dict[str, Counter] = {}
        titles: dict[str, list[str]] = {}
        q = "SELECT * FROM events WHERE deleted = 0 AND parent_id IS NULL AND teachers != '' AND category = ?"
        for r in self.conn.execute(q, (AUTO_TEACHER_CATEGORY,)):
            ev = Event.from_row(r)
            subj = ev.subject.strip() or next((w for w in words if w in ev.title), "")
            if not subj:
                continue
            found.setdefault(subj, Counter())[ev.teachers] += 1
            titles.setdefault(subj, []).append(ev.title)
        current = dict(self.subject_teachers())
        out = []
        for subj, cnt in sorted(found.items()):
            teacher, _ = cnt.most_common(1)[0]
            if current.get(subj) == teacher:
                continue
            out.append({"subject": subj, "teacher": teacher, "events": titles[subj],
                        "others": {t: n for t, n in cnt.items() if t != teacher}, "current": current.get(subj, "")})
        return out

    def _auto_teacher(self, title: str, subject: str = "") -> tuple[str, list[str]]:
        subj = self.match_subject(title, subject)
        if not subj:
            return subject, []
        row = self.conn.execute("SELECT teacher FROM subject_teachers WHERE subject = ?", (subj,)).fetchone()
        return subj, split_names(row[0]) if row else []

    def fill_teachers(self) -> int:
        """선생님이 비어 있는 Assignment 일정을 과목표로 채움. 채운 수 반환."""
        n = 0
        for e in self.all():
            if e.category == AUTO_TEACHER_CATEGORY and not e.teachers:
                subj, t_list = self._auto_teacher(e.title, e.subject)
                if t_list:
                    self.conn.execute("UPDATE events SET teachers = ?, subject = ?, updated_at = ? WHERE id = ?",
                                      (", ".join(t_list), subj, now_iso(), e.id))
                    n += 1
        self.conn.commit()
        return n

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
            category: str = "", location: str = "", notes: str = "", deadline: bool = False,
            teachers=None, subject: str = "", team=None) -> Event:
        title = clean_title(title)
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
        t_list = split_names(teachers)
        subject = (subject or "").strip()
        if category == AUTO_TEACHER_CATEGORY and not t_list:
            subject, t_list = self._auto_teacher(title, subject)
        ts = now_iso()
        cur = self.conn.execute(
            "INSERT INTO events (title, start, end, all_day, deadline, category, location, notes, teachers, subject,"
            " team, entered, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title.strip(), s, e, int(all_day), int(deadline), category or "", location or "", notes or "",
             ", ".join(t_list), subject, ", ".join(split_names(team)), date.today().isoformat(), ts, ts),
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
        if "teachers" in fields:
            fields["teachers"] = ", ".join(split_names(fields["teachers"]))
        if "team" in fields:
            fields["team"] = ", ".join(split_names(fields["team"]))
        if "title" in fields:
            fields["title"] = clean_title(fields["title"])
            if not fields["title"]:
                raise ValueError("제목이 비어 있음")
        if fields.get("attachment"):
            f = Path(fields["attachment"]).expanduser()
            if not f.is_file():
                raise ValueError(f"안내문 파일이 없음: {f}")
            fields["attachment"] = str(f.resolve())
        # Assignment인데 선생님이 비어 있으면 과목으로 자동 입력
        cat = fields.get("category", ev.category)
        teachers = fields.get("teachers", ev.teachers)
        if cat == AUTO_TEACHER_CATEGORY and not teachers and ({"category", "title", "subject", "teachers"} & set(fields)):
            subj, t_list = self._auto_teacher(fields.get("title", ev.title), fields.get("subject", ev.subject))
            if t_list:
                fields["teachers"] = ", ".join(t_list)
                fields["subject"] = subj
        if "all_day" in fields:
            fields["all_day"] = int(all_day)
            # 종일 여부가 바뀌면 시간 형식도 다시 맞춰야 함
            fields.setdefault("start", ev.start)
            fields.setdefault("end", ev.end)
        if "start" in fields:
            fields["start"] = normalize_time(fields["start"], all_day)
        if "end" in fields:
            fields["end"] = normalize_time(fields["end"], all_day)
        if fields.get("entered"):
            fields["entered"] = date.fromisoformat(str(fields["entered"])[:10]).isoformat()
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

    # ---------- 작업 세션 ----------
    def sessions_of(self, parent_id: int) -> list[Event]:
        q = "SELECT * FROM events WHERE parent_id = ? AND deleted = 0 ORDER BY start, id"
        return [Event.from_row(r) for r in self.conn.execute(q, (parent_id,))]

    def add_session(self, parent: Event, name: str, start: str, end: str, minutes: int) -> Event:
        ev = self.add(f"{parent.title} · {name}", start, end, category=parent.category,
                      teachers=parent.teachers, subject=parent.subject)
        return self.update(ev.id, parent_id=parent.id, est_minutes=minutes)

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
