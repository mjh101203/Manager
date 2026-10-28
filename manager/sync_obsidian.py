"""Obsidian 백업 — vault 안의 전용 폴더에 마크다운을 생성.

<vault>/<folder>/
  Index.md               다가오는 일정 + 카테고리별 요약
  Daily/YYYY-MM-DD.md    날짜별 노트 (일정이 있는 날만)

플러그인 없이 파일만 쓰므로 Obsidian이 꺼져 있어도 동작한다.
frontmatter에 `generator: manager`가 있는 파일만 덮어쓰기/삭제한다.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .config import ObsidianConfig
from .db import Event, Store

WEEKDAYS = "월화수목금토일"
MARKER = "generator: manager"
BANNER = "> [!info] Manager가 자동 생성한 파일 — 여기서 고쳐도 다음 동기화 때 덮어써짐"


def _tag(s: str) -> str:
    t = re.sub(r"[^\w/-]", "_", s.strip())
    return f"#{t}" if t else ""


def _line(e: Event) -> str:
    box = {"done": "x", "cancelled": "-"}.get(e.status, " ")
    title = f"~~{e.title}~~" if e.status == "cancelled" else f"**{e.title}**"
    parts = [f"- [{box}] {e.time_label()} {title}"]
    if e.location:
        parts.append(f"📍{e.location}")
    if e.category:
        parts.append(_tag(e.category))
    parts.append(f"`#{e.id}`")
    out = " ".join(parts)
    if e.notes:
        out += "\n" + "\n".join(f"    {ln}" for ln in e.notes.splitlines())
    return out


def _weekday(day: str) -> str:
    return WEEKDAYS[date.fromisoformat(day).weekday()]


def render_daily(day: str, events: list[Event]) -> str:
    cats = sorted({e.category for e in events if e.category})
    fm = ["---", MARKER, f"date: {day}", "tags:", "  - manager"]
    fm += [f"  - {_tag(c)[1:]}" for c in cats]
    fm.append("---")
    d = date.fromisoformat(day)
    nav = f"← [[{(d - timedelta(days=1)).isoformat()}]] · [[Index]] · [[{(d + timedelta(days=1)).isoformat()}]] →"
    body = [f"# {day} ({_weekday(day)})", "", BANNER, "", *(_line(e) for e in events), "", nav, ""]
    return "\n".join(fm + [""] + body)


def render_index(events: list[Event], today: date) -> str:
    t = today.isoformat()
    upcoming = [e for e in events if e.status == "todo" and e.day >= t
                and e.day < (today + timedelta(days=30)).isoformat()]
    ongoing = [e for e in events if e.status == "todo" and e.day < t and e.end and e.end[:10] >= t]
    overdue = [e for e in events if e.status == "todo" and (e.end or e.start)[:10] < t]
    lines = ["---", MARKER, "tags:", "  - manager", "---", "", "# 📅 Manager 일정", "", BANNER, "",
             f"_마지막 동기화: {today.isoformat()}_", ""]
    if overdue:
        lines += ["## ⚠️ 지난 미완료", ""]
        lines += [f"- [[Daily/{e.day}|{e.day}]] " + _line(e)[2:] for e in overdue]
        lines.append("")
    if ongoing:
        lines += ["## 🔄 진행 중", ""]
        lines += [f"- [[Daily/{e.day}|{e.day}]] ~ {e.end[:10]} " + _line(e)[2:] for e in ongoing]
        lines.append("")
    lines += ["## 다가오는 30일", ""]
    if not upcoming:
        lines.append("_(없음)_")
    by_day: dict[str, list[Event]] = defaultdict(list)
    for e in upcoming:
        by_day[e.day].append(e)
    for day, evs in by_day.items():
        lines += [f"### [[Daily/{day}|{day} ({_weekday(day)})]]", *(_line(e) for e in evs), ""]
    by_cat: dict[str, int] = defaultdict(int)
    for e in events:
        if e.status == "todo":
            by_cat[e.category or "(미분류)"] += 1
    if by_cat:
        lines += ["## 카테고리별 남은 일정", "", "| 카테고리 | 개수 |", "|---|---|"]
        lines += [f"| {c} | {n} |" for c, n in sorted(by_cat.items())]
        lines.append("")
    return "\n".join(lines)


def _is_generated(p: Path) -> bool:
    try:
        head = p.read_text(encoding="utf-8")[:200]
    except OSError:
        return False
    return head.startswith("---") and MARKER in head


def _write_if_changed(p: Path, content: str) -> bool:
    if p.exists():
        if not _is_generated(p):
            raise RuntimeError(f"직접 만든 파일로 보여서 덮어쓰지 않음: {p}")
        if p.read_text(encoding="utf-8") == content:
            return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    return True


def sync(store: Store, cfg: ObsidianConfig, today: date | None = None) -> dict:
    if not cfg.vault_path:
        raise RuntimeError("obsidian.vault_path 설정 필요")
    vault = Path(cfg.vault_path).expanduser()
    if not vault.is_dir():
        raise RuntimeError(f"vault 폴더 없음: {vault}")
    root = vault / cfg.folder
    daily_dir = root / "Daily"
    today = today or date.today()

    events = store.all()
    by_day: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_day[e.day].append(e)

    stats = {"written": 0, "removed": 0, "unchanged": 0}
    for day, evs in by_day.items():
        if _write_if_changed(daily_dir / f"{day}.md", render_daily(day, evs)):
            stats["written"] += 1
        else:
            stats["unchanged"] += 1
    # 일정이 다 사라진 날짜의 노트 정리 (자동 생성 파일만)
    if daily_dir.is_dir():
        for p in daily_dir.glob("*.md"):
            if p.stem not in by_day and _is_generated(p):
                p.unlink()
                stats["removed"] += 1
    if _write_if_changed(root / "Index.md", render_index(events, today)):
        stats["written"] += 1
    return stats
