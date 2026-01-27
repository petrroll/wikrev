from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class AppConfig:
    repo_path: Path
    last_run: Optional[datetime]
    enable_copilot: bool
    copilot_model: str
    default_weekday: str
    default_time: str
    path_filters: List[str]  # Glob patterns; prefix with ! to negate (include)
    sort_order: str  # "newest_first" or "oldest_first"


# Config is loaded from .wikrev directory in current working directory
WIKREV_DIR = Path.cwd() / ".wikrev"
CONFIG_PATH = WIKREV_DIR / "config.json"

DEFAULT_CONFIG = {
    "repo_path": ".",
    "enable_copilot": True,
    "copilot_model": "gpt-5",
    "default_weekday": "tuesday",
    "default_time": "15:00",
    "path_filters": [],
    "sort_order": "newest_first"
}


def init_config(path: Path = CONFIG_PATH) -> Path:
    """Create a default config file in the specified path."""
    if path.exists():
        raise FileExistsError(f"Config file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return path


def _parse_time(value: str) -> time:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("default_time must be HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    return time(hour=hour, minute=minute)


def _default_since(default_weekday: str, default_time: str) -> datetime:
    now = datetime.now().astimezone()
    target_weekday = WEEKDAY_INDEX.get(default_weekday.lower(), 1)
    target_time = _parse_time(default_time)

    today = now.date()
    days_since = (today.weekday() - target_weekday) % 7
    if days_since == 0:
        days_since = 7
    target_date = today - timedelta(days=days_since)
    target_dt = datetime.combine(target_date, target_time, tzinfo=now.tzinfo)

    if target_dt > now:
        target_dt -= timedelta(days=7)
    return target_dt


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Resolve repo_path relative to the parent of .wikrev (project root), not the .wikrev folder itself
    repo_path = (path.parent.parent / raw["repo_path"]).resolve()
    last_run = None
    if raw.get("last_run"):
        last_run = datetime.fromisoformat(raw["last_run"]).astimezone()
    if last_run is None:
        last_run = _default_since(raw.get("default_weekday", "tuesday"), raw.get("default_time", "15:00"))

    return AppConfig(
        repo_path=repo_path,
        last_run=last_run,
        enable_copilot=bool(raw.get("enable_copilot", True)),
        copilot_model=str(raw.get("copilot_model", "gpt-5")),
        default_weekday=str(raw.get("default_weekday", "tuesday")),
        default_time=str(raw.get("default_time", "15:00")),
        path_filters=list(raw.get("path_filters", raw.get("excluded_folders", []))),
        sort_order=str(raw.get("sort_order", "newest_first")),
    )


def save_last_run(value: datetime, path: Path = CONFIG_PATH) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["last_run"] = value.astimezone().isoformat()
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def save_sort_order(value: str, path: Path = CONFIG_PATH) -> None:
    """Save the sort order preference to config."""
    if value not in ("newest_first", "oldest_first"):
        raise ValueError(f"Invalid sort_order: {value}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["sort_order"] = value
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
