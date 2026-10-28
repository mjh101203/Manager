"""설정 로드. 우선순위: --config 인자 > MANAGER_CONFIG 환경변수 > ./config.toml > ~/.manager/config.toml"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


@dataclass
class ClaudeConfig:
    command: str = "claude"
    model: str = ""  # 비우면 claude CLI 기본값
    extra_args: list[str] = field(default_factory=list)
    timeout: int = 120
    history_turns: int = 6  # chat 모드에서 프롬프트에 넣을 이전 대화 수


@dataclass
class NotionConfig:
    enabled: bool = False
    token: str = ""
    database_id: str = ""


@dataclass
class ObsidianConfig:
    enabled: bool = False
    vault_path: str = ""
    folder: str = "Manager"


@dataclass
class Config:
    db_path: Path
    auto_sync: bool = True
    confirm: bool = True
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    notion: NotionConfig = field(default_factory=NotionConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    source: Path | None = None


def _candidates(explicit: str | None) -> list[Path]:
    paths = []
    if explicit:
        paths.append(Path(explicit))
    if os.environ.get("MANAGER_CONFIG"):
        paths.append(Path(os.environ["MANAGER_CONFIG"]))
    paths.append(Path.cwd() / "config.toml")
    paths.append(Path.home() / ".manager" / "config.toml")
    return paths


def load_config(explicit: str | None = None) -> Config:
    data: dict = {}
    source = None
    for p in _candidates(explicit):
        if p.is_file():
            with open(p, "rb") as f:
                data = tomllib.load(f)
            source = p
            break
    else:
        if explicit:
            raise FileNotFoundError(f"설정 파일 없음: {explicit}")

    base = source.parent if source else Path.home() / ".manager"
    db_raw = data.get("db_path", "manager.db")
    db_path = Path(os.path.expanduser(db_raw))
    if not db_path.is_absolute():
        db_path = base / db_path

    # Notion 토큰은 환경변수로도 받음 (설정 파일에 비밀값 안 남기고 싶을 때)
    n = dict(data.get("notion", {}))
    if not n.get("token") and os.environ.get("NOTION_TOKEN"):
        n["token"] = os.environ["NOTION_TOKEN"]

    return Config(
        db_path=db_path,
        auto_sync=data.get("auto_sync", True),
        confirm=data.get("confirm", True),
        claude=ClaudeConfig(**data.get("claude", {})),
        notion=NotionConfig(**n),
        obsidian=ObsidianConfig(**data.get("obsidian", {})),
        source=source,
    )
