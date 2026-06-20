"""
Weekly Telegram delivery.

Formats and sends two consecutive messages:
  1. Weekly summary (what was accomplished this week)
  2. Weekly plan (goals per project for next week)

Public API
----------
format_weekly_summary_message(weekly_plan) -> str   MarkdownV2 string
format_weekly_plan_message(weekly_plan) -> str       MarkdownV2 string
send_weekly_messages(weekly_plan) -> None            Sends both messages
"""
from __future__ import annotations

import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from delivery.telegram_send import _esc, _send_md
from utils.logger import get_logger

log = get_logger("telegram_weekly")

_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def format_weekly_summary_message(weekly_plan: dict) -> str:
    """Format the weekly summary as a MarkdownV2 Telegram message."""
    week = weekly_plan.get("week", "")
    week_start = weekly_plan.get("week_start", "")
    week_end = weekly_plan.get("week_end", "")
    summary = weekly_plan.get("week_summary", "")
    highlights = weekly_plan.get("highlights") or []

    lines: list[str] = [
        f"📊 *Weekly Summary — {_esc(week)}*",
        f"_{_esc(week_start)} → {_esc(week_end)}_",
        "",
        _esc(summary),
    ]

    if highlights:
        lines += ["", "✅ *Highlights:*"]
        for h in highlights[:6]:
            lines.append(f"• {_esc(h)}")

    return "\n".join(lines)


def format_weekly_plan_message(weekly_plan: dict) -> str:
    """Format the weekly plan as a MarkdownV2 Telegram message."""
    week = weekly_plan.get("week", "")
    # Compute next week label
    try:
        year, wnum = week.split("-W")
        # Simple increment — ISO week overflow handled by date math
        week_start = datetime.date.fromisoformat(weekly_plan["week_start"])
        next_start = week_start + datetime.timedelta(weeks=1)
        iso = next_start.isocalendar()
        next_week = f"{iso.year}-W{iso.week:02d}"
    except Exception:
        next_week = "next week"

    projects = weekly_plan.get("projects") or []

    lines: list[str] = [
        f"📋 *Weekly Plan — {_esc(next_week)}*",
        "",
    ]

    if not projects:
        lines.append("_No projects planned\\._")
        return "\n".join(lines)

    for project in projects:
        project_name = project.get("project_name", "")
        goals = project.get("weekly_goals") or []
        lines += [f"*{_esc(project_name)}:*"]
        for goal_item in goals:
            goal = goal_item.get("goal", "")
            priority = goal_item.get("priority", "medium").lower()
            emoji = _PRIORITY_EMOJI.get(priority, "🟡")
            lines.append(f"{emoji} {_esc(goal)}")
        lines.append("")

    return "\n".join(lines).rstrip()


def send_weekly_messages(weekly_plan: dict) -> None:
    """Send weekly summary and plan as two consecutive Telegram messages."""
    summary_msg = format_weekly_summary_message(weekly_plan)
    plan_msg = format_weekly_plan_message(weekly_plan)

    log.info("Sending weekly summary (%d chars)", len(summary_msg))
    _send_md(summary_msg)

    log.info("Sending weekly plan (%d chars)", len(plan_msg))
    _send_md(plan_msg)
