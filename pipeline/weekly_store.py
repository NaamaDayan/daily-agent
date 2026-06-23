"""
Weekly plan store.

Persists weekly plans as JSON under config["weekly_plans_dir"].
Files are named YYYY-WNN.json (ISO week format).

Public API
----------
save_weekly_plan(plan, week_start)    Write plan to disk.
load_weekly_plan(week_start=None)     Load plan (None if not found).
get_current_weekly_plan()             Load the most recent weekly plan.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("weekly_store")


def _weekly_plans_dir() -> pathlib.Path:
    cfg = get_config()
    d = pathlib.Path(
        cfg.get("weekly_plans_dir", "~/.daily-agent/weekly-plans")
    ).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _week_key(week_start: datetime.date) -> str:
    """Return ISO week string like '2026-W25'."""
    iso = week_start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _plan_path(week_start: datetime.date) -> pathlib.Path:
    return _weekly_plans_dir() / f"{_week_key(week_start)}.json"


def save_weekly_plan(plan: dict, week_start: datetime.date) -> None:
    """Write weekly plan to disk for the week starting on *week_start*."""
    path = _plan_path(week_start)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Weekly plan saved: %s (%d projects)", path.name, len(plan.get("projects", [])))


def load_weekly_plan(week_start: datetime.date | None = None) -> dict | None:
    """Load weekly plan for *week_start*. Returns None if file does not exist."""
    if week_start is None:
        # Default: find the Monday of the current week
        today = datetime.date.today()
        week_start = today - datetime.timedelta(days=today.weekday())
    path = _plan_path(week_start)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to read weekly plan %s: %s", path.name, exc)
        return None


def get_current_weekly_plan() -> dict | None:
    """
    Return the most recently generated weekly plan, or None if none exists.

    Scans weekly_plans_dir for the newest file by ISO week name.
    """
    d = _weekly_plans_dir()
    files = sorted(d.glob("????-W??.json"), reverse=True)
    for path in files:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to read %s: %s", path.name, exc)
    return None
