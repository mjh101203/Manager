"""과제 수행 일정 플래너.

역할 분담
  - AI (스킬: skills/assignment-planner/SKILL.md)
      과제 유형·난이도 판단, 총 소요 시간 추정, 단계 분해, 배치 성향(strategy)
  - Python (이 파일)
      가용 시간표 − 기존 일정 = 빈 시간 계산, 세션 배치(겹침·하루 상한·마감 여유 보장),
      내 기록으로 추정 보정(유형별 실제/예상 비율)

설정: planner.toml (없으면 기본값)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .db import Event, Store

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS = "월화수목금토일"
COMPLEXITY_FACTOR = {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.4, 5: 2.0}
_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATHS = [_ROOT / "skills" / "assignment-planner" / "SKILL.md",
               _ROOT / ".claude" / "skills" / "assignment-planner" / "SKILL.md"]

DEFAULT_PROFILE = {
    "availability": {
        "mon": "19:30-23:00", "tue": "19:30-23:00", "wed": "19:30-23:00", "thu": "19:30-23:00",
        "fri": "19:30-22:00", "sat": "10:00-12:00, 14:00-18:00", "sun": "14:00-21:00",
    },
    "session": {"min_minutes": 30, "max_minutes": 120, "max_per_day_minutes": 180,
                "break_minutes": 10, "buffer_hours": 12, "all_day_deadline_time": "00:00",
                "daily_reserve_minutes": 120,
                "latest_end": "22:00",               # 이 시각 이후로는 과제 안 잡음
                "home_weekend_latest_end": "24:00"}, # 단, 귀가주 금·토는 이 시각까지
    "estimates": {"보고서": 240, "발표": 180, "문제풀이": 90, "독서감상문": 120,
                  "자료조사": 90, "실험": 180, "암기": 60, "기타": 120},
    "skills": {},   # 영역별 역량 1~10 (1 = 평균적인 고등학생, 10 = 대학원생)
    "me": {"note": ""},
}


# ---------------- 프로필 ----------------
@dataclass
class Profile:
    availability: dict[str, list[tuple[time, time | None]]]   # None = 24:00
    min_minutes: int = 30
    max_minutes: int = 120
    max_per_day_minutes: int = 180
    break_minutes: int = 10
    buffer_hours: float = 12
    all_day_deadline_time: time = time(0, 0)
    estimates: dict[str, int] = field(default_factory=dict)
    skills: dict[str, int] = field(default_factory=dict)
    daily_reserve_minutes: int = 120
    latest_end: time | None = time(22, 0)        # None = 24:00 (제한 없음)
    home_weekend_latest_end: time | None = None
    stay_dates: set = field(default_factory=set)  # 학사일정 '미귀가' 날짜 (school.py)
    note: str = ""
    source: Path | None = None


def _parse_hm(s: str) -> time | None:
    s = s.strip()
    if s in ("24:00", "24"):
        return None
    h, m = (s.split(":") + ["0"])[:2]
    return time(int(h), int(m))


def parse_windows(spec: str) -> list[tuple[time, time | None]]:
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        start, end = _parse_hm(a), _parse_hm(b)
        if start is None:
            raise ValueError(f"잘못된 시간대: {part}")
        out.append((start, end))
    return out


def load_profile(path: Path | None) -> Profile:
    data = json.loads(json.dumps(DEFAULT_PROFILE))  # deep copy
    source = None
    if path and path.is_file():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and k in data:
                data[k].update(v)
            else:
                data[k] = v
        source = path
    s = data["session"]
    return Profile(
        availability={k: parse_windows(data["availability"].get(k, "")) for k in DAY_KEYS},
        min_minutes=int(s["min_minutes"]), max_minutes=int(s["max_minutes"]),
        max_per_day_minutes=int(s["max_per_day_minutes"]), break_minutes=int(s["break_minutes"]),
        buffer_hours=float(s["buffer_hours"]),
        all_day_deadline_time=_parse_hm(str(s["all_day_deadline_time"])) or time(0, 0),
        estimates={k: int(v) for k, v in data["estimates"].items()},
        skills={k: max(1, min(10, int(v))) for k, v in data.get("skills", {}).items()},
        daily_reserve_minutes=int(s.get("daily_reserve_minutes", 120)),
        latest_end=_parse_hm(str(s.get("latest_end", "22:00"))),
        home_weekend_latest_end=_parse_hm(str(s.get("home_weekend_latest_end", "24:00"))),
        note=str(data.get("me", {}).get("note", "")), source=source,
    )


# ---------------- 시간 계산 ----------------
def _local_tz():
    return datetime.now().astimezone().tzinfo


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=_local_tz())


def parse_dt(s: str) -> datetime:
    if len(s) == 10:
        return _aware(datetime.combine(date.fromisoformat(s), time(0, 0)))
    return _aware(datetime.fromisoformat(s))


def due_of(e: Event, profile: Profile) -> datetime:
    """과제 마감 시각. 종일 마감은 그날 all_day_deadline_time (기본 00:00 = 전날 밤까지)."""
    ref = e.end or e.start
    if len(ref) == 10:
        return _aware(datetime.combine(date.fromisoformat(ref), profile.all_day_deadline_time))
    return parse_dt(ref)


def busy_intervals(store: Store, start: datetime, end: datetime, exclude_ids: set[int]) -> list[tuple[datetime, datetime]]:
    """시간이 정해진 일정만 바쁨으로 봄 (종일·마감 표시용 일정은 제외)."""
    out = []
    for e in store.range(start.date().isoformat(), (end.date() + timedelta(days=1)).isoformat()):
        if e.id in exclude_ids or e.all_day or e.status == "cancelled":
            continue
        if e.deadline:  # 마감 일정은 기간이 있어도 그 시간 동안 바쁜 게 아님
            continue
        s = parse_dt(e.start)
        t = parse_dt(e.end) if e.end else s + timedelta(minutes=60)
        if t > start and s < end:
            out.append((s, t))
    return sorted(out)


def free_slots(profile: Profile, busy: list[tuple[datetime, datetime]],
               start: datetime, end: datetime) -> list[list[datetime]]:
    """[start, end) 안에서 가용 시간표 − 바쁜 시간."""
    from .school import is_home_weekend_day
    slots: list[list[datetime]] = []
    d = start.date()

    def at(day: date, t: time | None) -> datetime:
        return _aware(datetime.combine(day + timedelta(days=1), time(0, 0))) if t is None else _aware(datetime.combine(day, t))

    while d <= end.date():
        # 늦은 시간 제한: 평소 latest_end, 귀가주 금·토는 home_weekend_latest_end
        limit = profile.home_weekend_latest_end if is_home_weekend_day(d, profile.stay_dates) else profile.latest_end
        cutoff = at(d, limit)
        for a, b in profile.availability[DAY_KEYS[d.weekday()]]:
            ws = _aware(datetime.combine(d, a))
            we = at(d, b)
            ws, we = max(ws, start), min(we, end, cutoff)
            if we <= ws:
                continue
            pieces = [[ws, we]]
            for bs, be in busy:
                nxt = []
                for ps, pe in pieces:
                    if be <= ps or bs >= pe:
                        nxt.append([ps, pe])
                        continue
                    if bs > ps:
                        nxt.append([ps, bs])
                    if be < pe:
                        nxt.append([be, pe])
                pieces = nxt
            slots += [p for p in pieces if (p[1] - p[0]) >= timedelta(minutes=profile.min_minutes // 2)]
        d += timedelta(days=1)
    return sorted(slots)


def _ceil_to(dt: datetime, minutes: int = 30) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    r = dt.minute % minutes
    return dt if r == 0 else dt + timedelta(minutes=minutes - r)


# ---------------- 보정 ----------------
def calibration(store: Store) -> dict[str, dict]:
    """유형별 (완료된 과제 수, 예상 합, 실제 합, 비율). '전체' 키 포함."""
    stats: dict[str, dict] = {}

    def add(key: str, est: float, act: float) -> None:
        s = stats.setdefault(key, {"n": 0, "est": 0, "actual": 0})
        s["n"] += 1
        s["est"] += round(est)
        s["actual"] += round(act)

    rows = store.conn.execute(
        "SELECT task_type, est_minutes, actual_minutes, components FROM events WHERE deleted = 0 "
        "AND parent_id IS NULL AND est_minutes > 0 AND actual_minutes > 0")
    for t, est, act, comps in rows:
        add(t or "기타", est, act)
        add("전체", est, act)
        # 작업 요소별: 과제 전체의 실제/예상 비율을 요소 몫만큼 나눠 반영
        try:
            parts = [c for c in json.loads(comps or "[]") if int(c.get("minutes", 0)) > 0]
        except (ValueError, TypeError):
            parts = []
        total = sum(int(c["minutes"]) for c in parts)
        for c in parts:
            share = int(c["minutes"]) / total * est if total else 0
            add(f"요소:{c['name']}", share, share * act / est)
    for s in stats.values():
        s["ratio"] = round(min(2.5, max(0.4, s["actual"] / s["est"])), 2)
    return stats


def suggested_minutes(profile: Profile, calib: dict, task_type: str, complexity: int) -> int:
    base = profile.estimates.get(task_type, profile.estimates.get("기타", 120))
    ratio = (calib.get(task_type) or calib.get("전체") or {"ratio": 1.0})["ratio"]
    return int(round(base * COMPLEXITY_FACTOR.get(complexity, 1.0) * ratio / 15) * 15)


# ---------------- 분해 → 조각 ----------------
@dataclass
class Chunk:
    name: str
    minutes: int


def make_chunks(steps: list[dict], profile: Profile) -> list[Chunk]:
    mx, mn = profile.max_minutes, profile.min_minutes
    raw: list[Chunk] = []
    for st in steps:
        name, m = str(st.get("name", "작업")).strip() or "작업", int(st.get("minutes", 0))
        if m <= 0:
            continue
        k = max(1, math.ceil(m / mx))
        size = int(math.ceil(m / k / 5) * 5)
        for i in range(k):
            part = min(size, m - size * i)
            raw.append(Chunk(f"{name} ({i + 1}/{k})" if k > 1 else name, part))
    # 너무 짧은 조각은 다음 조각과 합침
    out: list[Chunk] = []
    for c in raw:
        if out and (out[-1].minutes < mn or c.minutes < mn) and out[-1].minutes + c.minutes <= mx:
            out[-1] = Chunk(f"{out[-1].name} + {c.name}", out[-1].minutes + c.minutes)
        else:
            out.append(c)
    return out


# ---------------- 배치 ----------------
@dataclass
class Placed:
    name: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


_PIECE_RE = None


def base_name(name: str) -> str:
    """'초안 (2/3) ② ①' → '초안'"""
    import re
    global _PIECE_RE
    _PIECE_RE = _PIECE_RE or re.compile(r"(\s*[①②])+$|\s*\(\d+/\d+\)$")
    prev = None
    while prev != name:
        prev, name = name, _PIECE_RE.sub("", name).strip()
    return name


def tidy(placed: list[Placed], profile: Profile) -> list[Placed]:
    """같은 날 이어지는 조각은 합치고(최대 세션 길이 안에서), 이름을 '단계 (i/n)'으로 정리."""
    merged: list[tuple[datetime, int, list[str]]] = []  # (시작, 분, 단계 이름들)
    gap = timedelta(minutes=profile.break_minutes + 10)
    for p in placed:
        b = base_name(p.name)
        if merged:
            st, m, names = merged[-1]
            prev_end = st + timedelta(minutes=m)
            if (st.date() == p.start.date() and p.start - prev_end <= gap
                    and m + p.minutes <= profile.max_minutes):
                merged[-1] = (st, m + p.minutes, names + ([b] if b not in names else []))
                continue
        merged.append((p.start, p.minutes, [b]))
    labels = [" + ".join(n) for _, _, n in merged]
    total = {lb: labels.count(lb) for lb in labels}
    seen: dict[str, int] = {}
    out = []
    for (st, m, _), lb in zip(merged, labels):
        seen[lb] = seen.get(lb, 0) + 1
        name = f"{lb} ({seen[lb]}/{total[lb]})" if total[lb] > 1 else lb
        out.append(Placed(name, st, st + timedelta(minutes=m)))
    return out


def _merge_pieces(chunks: list[Chunk]) -> list[Chunk]:
    """못 넣은 작업을 단계 이름 기준으로 합쳐서 보고."""
    out: list[Chunk] = []
    for c in chunks:
        b = base_name(c.name)
        if out and out[-1].name == b:
            out[-1] = Chunk(b, out[-1].minutes + c.minutes)
        else:
            out.append(Chunk(b, c.minutes))
    return out


def pack(chunks: list[Chunk], slots: list[list[datetime]], profile: Profile,
         strategy: dict | None = None, used_per_day: dict[date, int] | None = None) -> tuple[list[Placed], list[Chunk]]:
    """순서를 지키며 빈 시간에 배치. (배치됨, 못 넣은 조각)."""
    strategy = strategy or {}
    slots = [list(s) for s in slots]
    used = dict(used_per_day or {})
    avoid = {date.fromisoformat(d) for d in strategy.get("avoid_dates", []) if d}
    prefer = strategy.get("prefer", "any")

    def allowed(d: date) -> bool:
        return d not in avoid

    days = sorted({s[0].date() for s in slots if allowed(s[0].date())})
    if prefer in ("weekend", "weekday"):
        want = [d for d in days if (d.weekday() >= 5) == (prefer == "weekend")]
        if len(want) >= max(1, math.ceil(len(chunks) / 2)):
            days = want
    spread = strategy.get("spread", "even")
    placed: list[Placed] = []
    failed: list[Chunk] = []
    prev_end: datetime | None = None
    brk = timedelta(minutes=profile.break_minutes)
    # 매일 예비 시간: 그날 빈 시간 중 daily_reserve_minutes 는 남겨둠
    free_total: dict[date, int] = {}
    for a, b in slots:
        free_total[a.date()] = free_total.get(a.date(), 0) + int((b - a).total_seconds() // 60)
    new_use: dict[date, int] = {}

    # 목표 날짜: 날짜 개수가 아니라 '날짜별 넣을 수 있는 분'에 비례해서 나눔
    day_cap = {d: max(0, min(profile.max_per_day_minutes - used.get(d, 0),
                             free_total.get(d, 0) - profile.daily_reserve_minutes)) for d in days}
    total_cap = sum(day_cap.values())
    total_need = sum(c.minutes for c in chunks)
    if spread == "even" and total_cap and total_need / total_cap > 0.6:
        spread = "front"  # 빠듯하면 고르게 펼치지 말고 앞에서부터 채움
    cum, acc = [], 0
    for d in days:
        acc += day_cap[d]
        cum.append((d, acc))

    def target(need_before: int) -> date | None:
        if not days or not total_need:
            return None
        if spread == "front":
            return days[0]
        point = (max(0, total_cap - total_need + need_before) if spread == "back"
                 else need_before / total_need * total_cap)
        for d, c in cum:
            if c > point:
                return d
        return days[-1]
    need_before = 0

    def fits_day(d: date, minutes: int) -> bool:
        if used.get(d, 0) + minutes > profile.max_per_day_minutes:
            return False
        return new_use.get(d, 0) + minutes <= free_total.get(d, 0) - profile.daily_reserve_minutes
    queue = list(chunks)
    mn = profile.min_minutes

    def day_left(d: date) -> int:
        return min(profile.max_per_day_minutes - used.get(d, 0),
                   free_total.get(d, 0) - profile.daily_reserve_minutes - new_use.get(d, 0))

    def floor5(x: int) -> int:
        return x - x % 5

    while queue:
        c = queue.pop(0)
        spot = None
        for tgt in (target(need_before), None):  # 목표 날짜부터, 안 되면 아무 날
            for s in slots:
                d = s[0].date()
                if not allowed(d) or (prefer in ("weekend", "weekday") and d not in days and tgt is not None):
                    continue
                if tgt and d < tgt:
                    continue
                st = _ceil_to(max(s[0], prev_end + brk if prev_end else s[0]), 10)
                room = min(int((s[1] - st).total_seconds() // 60), day_left(d))
                if room >= c.minutes:
                    take = c.minutes
                else:
                    # 그날 남은 만큼 잘라서 채우기 (양쪽 다 최소 세션 길이 이상일 때)
                    take = min(floor5(room), c.minutes - mn)
                    if take < mn:
                        continue
                spot = (s, st, take)
                break
            if spot:
                break
        if not spot:
            # 순서가 뒤집히지 않게 남은 작업은 전부 '시간 부족'
            failed = _merge_pieces([c] + queue)
            break
        s, st, take = spot
        name = c.name
        if take < c.minutes:
            name = f"{c.name} ①"
            queue.insert(0, Chunk(f"{c.name} ②", c.minutes - take))
        need_before += take
        p = Placed(name, st, st + timedelta(minutes=take))
        placed.append(p)
        used[st.date()] = used.get(st.date(), 0) + take
        new_use[st.date()] = new_use.get(st.date(), 0) + take + profile.break_minutes
        s[0] = p.end + brk
        prev_end = p.end
    return tidy(placed, profile), failed


# ---------------- AI ----------------
def skill_text() -> str:
    for path in SKILL_PATHS:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            break
    else:
        return ""
    if text.startswith("---"):
        text = text.split("---", 2)[-1]
    return text.strip()


def build_plan_prompt(store: Store, ev: Event, profile: Profile, calib: dict, due: datetime,
                      free_minutes_by_day: dict[date, int], done_minutes: int,
                      pending: dict | None = None, feedback: str = "", now: datetime | None = None,
                      allow_questions: bool = True) -> str:
    now = now or datetime.now().astimezone()
    est_table = "\n".join(f"- {k}: {v}분" for k, v in profile.estimates.items())
    skills = "\n".join(f"- {k}: {v}/10" for k, v in profile.skills.items()) or "(설정 안 됨 — 모두 3으로 가정)"
    cal = "\n".join(f"- {k}: {v['n']}건, 예상 {v['est']}분 → 실제 {v['actual']}분 (비율 {v['ratio']})"
                    for k, v in calib.items()) or "(아직 기록 없음 — 비율 1.0으로 가정)"
    cap = "\n".join(f"- {d.isoformat()}({WEEKDAYS[d.weekday()]}): {m}분" for d, m in sorted(free_minutes_by_day.items()))
    total_free = sum(free_minutes_by_day.values())
    attach = ""
    if ev.attachment:
        attach = (f"- 과제 안내문 파일: {Path(ev.attachment).expanduser().resolve()}\n"
                  "  → 먼저 Read 도구로 이 파일을 읽고 분량·형식·평가 기준을 확인해.\n")
    rev = ""
    if feedback and not pending:
        rev = f"\n## 사용자 요청\n{feedback}\n"
    if pending and feedback:
        rev = (f"\n## 직전 계획 (아직 적용 안 됨)\n{json.dumps(pending, ensure_ascii=False)}\n\n"
               f"## 수정 요청\n{feedback}\n→ 수정 요청을 반영한 최종 계획 전체를 다시 출력해.\n")
    q_rule = ("정보가 부족해서 난이도를 판단할 수 없으면 steps는 비우고 questions에 질문 1~3개만 넣어."
              if allow_questions else "질문은 하지 마 (questions는 빈 배열). 부족한 정보는 합리적으로 가정하고 reply에 가정을 밝혀.")
    return f"""{skill_text()}

## 출력 형식 (JSON 객체 하나만, 다른 텍스트 금지)
{{"reply": "<왜 이 난이도·시간인지 1~3문장, 반말>",
 "questions": ["<정보가 부족할 때만>"],
 "task_type": "<유형>", "complexity": <1~5>,
 "components": [{{"name": "<작업 요소, 역량 목록의 이름>", "level_needed": <1~10>, "minutes": <int>}}],
 "total_minutes": <int>,
 "steps": [{{"name": "<단계 이름>", "minutes": <int>}}],
 "strategy": {{"spread": "even|front|back", "prefer": "any|weekday|weekend", "avoid_dates": ["YYYY-MM-DD"]}}}}
{q_rule}

## 현재 시각
{now.isoformat(timespec='minutes')} ({WEEKDAYS[now.weekday()]})

## 과제
- 제목: {ev.title}
- 주제: {ev.topic or '(없음)'}
- 범위·요구사항: {ev.scope or '(없음)'}
- 태그: {ev.category or '-'} / 과목: {ev.subject or '-'} / 선생님: {ev.teachers or '-'}
- 팀원: {ev.team or '(개인)'} — 팀 과제면 내 몫(나눌 수 있는 작업)만 계산하고, 팀 회의·취합 단계를 넣어
- 마감: {due.isoformat(timespec='minutes')} ({WEEKDAYS[due.weekday()]})
- 메모: {ev.notes or '-'}
{attach}- 이미 끝낸 작업 세션: {done_minutes}분

## 내 역량 (1 = 평균적인 고등학생, 10 = 대학원생)
{skills}

## 유형별 기본 소요 시간 (평균적인 고등학생 기준, 형식 참고용)
{est_table}

## 세션 규칙
- 세션 {profile.min_minutes}~{profile.max_minutes}분, 하루 과제 최대 {profile.max_per_day_minutes}분
- 매일 최소 {profile.daily_reserve_minutes}분은 예비 시간으로 비워둠 (아래 빈 시간은 이미 뺀 값)
- 나에 대한 메모: {profile.note or '-'}

## 내 기록 (예상 대비 실제) — '요소:' 항목은 작업 요소별
{cal}

## 마감 전 작업 가능 시간 (날짜별, 합계 {total_free}분)
{cap or '(없음)'}
{rev}"""


def parse_plan(text: str) -> dict:
    from .ai import extract_json
    data = extract_json(text)
    data["questions"] = [str(q) for q in data.get("questions") or [] if str(q).strip()]
    data["steps"] = [s for s in data.get("steps", []) if isinstance(s, dict) and int(s.get("minutes", 0)) > 0]
    data["components"] = [c for c in data.get("components", []) if isinstance(c, dict) and int(c.get("minutes", 0)) > 0]
    data["complexity"] = int(data.get("complexity") or 3)
    data["total_minutes"] = int(data.get("total_minutes") or sum(int(s["minutes"]) for s in data["steps"]))
    data.setdefault("strategy", {})
    return data


# ---------------- 전체 흐름 ----------------
@dataclass
class PlanResult:
    event: Event
    due: datetime
    plan: dict
    placed: list[Placed]
    failed: list[Chunk]
    replace_ids: list[int]
    done_minutes: int

    @property
    def questions(self) -> list[str]:
        return [] if self.plan.get("steps") else self.plan.get("questions", [])


def prepare(store: Store, event_id: int, profile: Profile, now: datetime | None = None):
    """계획에 필요한 계산값 (과제, 마감, 빈 시간, 교체할 세션 id, 이미 한 분량)."""
    ev = store.get(event_id)
    if not ev:
        raise KeyError(f"#{event_id} 일정 없음")
    if ev.parent_id:
        raise ValueError(f"#{event_id}는 작업 세션이야. 원래 과제(#{ev.parent_id})로 계획해줘.")
    now = now or datetime.now().astimezone()
    due = due_of(ev, profile)
    until = due - timedelta(hours=profile.buffer_hours)
    start = _ceil_to(now + timedelta(minutes=10), 30)
    if until <= start:
        raise ValueError(f"마감({due:%m-%d %H:%M})까지 여유 {profile.buffer_hours:g}시간을 빼면 남는 시간이 없어.")
    sessions = store.sessions_of(ev.id)
    replace = [s.id for s in sessions if s.status == "todo" and parse_dt(s.start) >= now]
    done_minutes = sum(s.est_minutes or 0 for s in sessions if s.status == "done")
    busy = busy_intervals(store, start, until, set(replace) | {ev.id})
    slots = free_slots(profile, busy, start, until)
    used: dict[date, int] = {}
    for e in store.range(start.date().isoformat(), (until.date() + timedelta(days=1)).isoformat()):
        if e.parent_id and e.id not in replace and e.est_minutes and e.status != "cancelled":
            d = parse_dt(e.start).date()
            used[d] = used.get(d, 0) + e.est_minutes
    by_day: dict[date, int] = {}
    for a, b in slots:
        by_day[a.date()] = by_day.get(a.date(), 0) + int((b - a).total_seconds() // 60)
    by_day = {d: max(0, min(m - profile.daily_reserve_minutes, profile.max_per_day_minutes - used.get(d, 0)))
              for d, m in by_day.items()}
    by_day = {d: m for d, m in by_day.items() if m > 0}
    return ev, due, slots, used, by_day, replace, done_minutes


def make_plan(store: Store, event_id: int, profile: Profile, call_ai, pending: dict | None = None,
              feedback: str = "", now: datetime | None = None, allow_questions: bool = True) -> PlanResult:
    """call_ai(prompt, read_files) -> str. AI로 분해하고 Python으로 배치."""
    ev, due, slots, used, by_day, replace, done_min = prepare(store, event_id, profile, now)
    calib = calibration(store)
    prompt = build_plan_prompt(store, ev, profile, calib, due, by_day, done_min, pending, feedback, now,
                               allow_questions)
    files = [ev.attachment] if ev.attachment else []
    plan = parse_plan(call_ai(prompt, files))
    chunks = make_chunks(plan["steps"], profile)
    placed, failed = pack(chunks, slots, profile, plan.get("strategy"), used)
    return PlanResult(ev, due, plan, placed, failed, replace, done_min)


def apply_plan(store: Store, r: PlanResult) -> list[Event]:
    for sid in r.replace_ids:
        store.delete(sid)
    created = [store.add_session(r.event, p.name, p.start.isoformat(timespec="minutes"),
                                 p.end.isoformat(timespec="minutes"), p.minutes) for p in r.placed]
    comps = [{"name": c.get("name"), "minutes": int(c["minutes"])} for c in r.plan.get("components", [])]
    store.update(r.event.id, est_minutes=int(r.plan["total_minutes"]) + r.done_minutes,
                 task_type=str(r.plan.get("task_type") or ""), complexity=int(r.plan["complexity"]),
                 components=json.dumps(comps, ensure_ascii=False) if comps else "")
    return created


def describe(r: PlanResult) -> list[str]:
    p = r.plan
    lines = [f"#{r.event.id} {r.event.title}" + (f" — 주제: {r.event.topic}" if r.event.topic else ""),
             f"마감 {r.due:%m/%d}({WEEKDAYS[r.due.weekday()]}) {r.due:%H:%M} · 유형 {p.get('task_type') or '-'} · "
             f"난이도 {p['complexity']}/5 · 예상 {p['total_minutes']}분"
             + (f" (이미 {r.done_minutes}분 함)" if r.done_minutes else "")]
    if p.get("components"):
        lines.append("작업 요소: " + ", ".join(
            f"{c.get('name')}" + (f"(Lv{c['level_needed']})" if c.get("level_needed") else "") + f" {c['minutes']}분"
            for c in p["components"]))
    for i, s in enumerate(r.placed, 1):
        lines.append(f"  {i}. {s.start:%m/%d}({WEEKDAYS[s.start.weekday()]}) {s.start:%H:%M}–{s.end:%H:%M}"
                     f"  {s.name}  ({s.minutes}분)")
    if r.failed:
        lines.append(f"  ⚠ 시간이 모자라서 못 넣은 작업: " + ", ".join(f"{c.name}({c.minutes}분)" for c in r.failed))
    if r.replace_ids:
        lines.append(f"  (기존 예정 세션 {len(r.replace_ids)}개는 이 계획으로 교체)")
    return lines


def parse_duration(s: str) -> int:
    """'3h', '2h30m', '90m', '1.5h', '90' → 분."""
    s = s.strip().lower().replace(" ", "").replace("시간", "h").replace("분", "m")
    if s.isdigit():
        return int(s)
    total, num = 0.0, ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch == "h":
            total += float(num or 0) * 60
            num = ""
        elif ch == "m":
            total += float(num or 0)
            num = ""
        else:
            raise ValueError(f"시간 형식 이해 못 함: {s}")
    if num:
        total += float(num)
    return int(round(total))
