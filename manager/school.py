"""학교 학사일정 (충북과학고 홈페이지) — 미귀가 주말 판별용.

페이지 구조 (school.cbe.go.kr/<학교>/M010407/list?y=YYYY&m=M):
    <li><dl><dt>26.11.28</dt><dd class="tch-tit"><div class="tch-tit-wrap">- 미귀가</div></dd>…</dl></li>
기간 일정은 <dt>26.12.01~26.12.04</dt>.

'미귀가'는 토요일 칸에 적힘. 그 주 금·토 중 하나라도 '미귀가'면 미귀가주, 아니면 귀가주.
결과는 school_calendar.json(설정 폴더)에 캐시.
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_URL = "https://school.cbe.go.kr/cbs-h/M010407/list"
_ITEM_RE = re.compile(
    r"<dt>\s*(\d{2}\.\d{2}\.\d{2})(?:\s*~\s*(\d{2}\.\d{2}\.\d{2}))?\s*</dt>\s*"
    r"<dd[^>]*class=\"[^\"]*tch-tit[^\"]*\"[^>]*>(.*?)</dd>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _d(s: str) -> date:
    y, m, d = s.split(".")
    return date(2000 + int(y), int(m), int(d))


def parse_page(html: str) -> list[dict]:
    """페이지 HTML → [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "title": str}]"""
    out = []
    for a, b, body in _ITEM_RE.findall(html):
        title = htmlmod.unescape(_TAG_RE.sub("", body)).strip().lstrip("-").strip()
        if title:
            out.append({"start": _d(a).isoformat(), "end": _d(b or a).isoformat(), "title": title})
    return out


def fetch_month(url: str, y: int, m: int, timeout: int = 15) -> list[dict]:
    req = urllib.request.Request(f"{url}?y={y}&m={m}", headers={"User-Agent": "Mozilla/5.0 (Manager)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_page(r.read().decode("utf-8", errors="replace"))


def fetch_range(url: str, start: date, months: int) -> list[dict]:
    """start가 속한 달부터 months개월. 페이지가 다른 달을 줘도 날짜가 붙어 있어서 합치면 됨."""
    seen, out = set(), []
    y, m = start.year, start.month
    for _ in range(months):
        for ev in fetch_month(url, y, m):
            key = (ev["start"], ev["end"], ev["title"])
            if key not in seen:
                seen.add(key)
                out.append(ev)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(out, key=lambda e: (e["start"], e["title"]))


# ---------------- 캐시 ----------------
def cache_path(base: Path) -> Path:
    return base / "school_calendar.json"


def load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"fetched_at": None, "events": []}


def save_cache(path: Path, events: list[dict]) -> dict:
    old = load_cache(path).get("events", [])
    merged = {(e["start"], e["end"], e["title"]): e for e in old + events}
    data = {"fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "events": sorted(merged.values(), key=lambda e: (e["start"], e["title"]))}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def is_stale(cache: dict, days: int = 7) -> bool:
    ts = cache.get("fetched_at")
    if not ts:
        return True
    return datetime.now().astimezone() - datetime.fromisoformat(ts) > timedelta(days=days)


def stay_dates(cache: dict, keyword: str = "미귀가") -> set[date]:
    out = set()
    for e in cache.get("events", []):
        if keyword in e["title"]:
            d, end = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
            while d <= end:
                out.add(d)
                d += timedelta(days=1)
    return out


def is_home_weekend_day(d: date, stays: set[date]) -> bool:
    """d가 귀가주의 금·토인지. (그 주 금·토에 '미귀가'가 없으면 귀가주)"""
    if d.weekday() not in (4, 5):
        return False
    fri = d - timedelta(days=d.weekday() - 4)
    return fri not in stays and fri + timedelta(days=1) not in stays
