"""
Telegram delivery for the daily plan (Stage 6 second message).

Formats the per-project plan from daily_planner.plan_tomorrow()
into a MarkdownV2 Telegram message and sends it.

Public API
----------
NO_WEEKLY_PLAN_MSG : str
    Plain-text fallback sent when no weekly plan exists.

format_daily_plan_message(plan, tomorrow) -> str
    Build the MarkdownV2 message string.

send_daily_plan(plan, tomorrow) -> None
    Format and send via Telegram Bot API.
"""
from __future__ import annotations

import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from delivery.telegram_send import _esc, _send_raw
from utils.logger import get_logger

log = get_logger("telegram_daily_plan")

NO_WEEKLY_PLAN_MSG = "⚠️ Weekly plan not found — skipping daily plan."


def format_daily_plan_message(
    plan: list[dict],
    tomorrow: datetime.date,
) -> str:
    """
    Format the daily plan as a MarkdownV2 Telegram message.

    plan : [{"project": str, "tasks": [str, ...]}, ...]
    """
    weekday = tomorrow.strftime("%a")
    day = tomorrow.strftime("%-d")
    month = tomorrow.strftime("%b")
    header = f"📅 *Plan for tomorrow — {_esc(weekday)} {_esc(day)} {_esc(month)}*"

    if not plan:
        return f"{header}\n\n_No projects planned\\._"

    sections: list[str] = [header]
    for entry in plan:
        project_name = entry["project"]
        tasks = entry.get("tasks", [])
        if not tasks:
            continue
        section_lines = [f"\n*{_esc(project_name)}:*"]
        for i, task in enumerate(tasks, 1):
            section_lines.append(f"{i}\\. {_esc(task)}")
        sections.append("\n".join(section_lines))

    return "\n".join(sections)


def send_daily_plan(plan: list[dict], tomorrow: datetime.date) -> None:
    """Format and send the daily plan as a Telegram MarkdownV2 message."""
    msg = format_daily_plan_message(plan, tomorrow)
    log.info("Sending daily plan (%d chars, %d projects)", len(msg), len(plan))
    _send_raw(msg, parse_mode="MarkdownV2")
