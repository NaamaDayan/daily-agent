"""
Weekly pipeline orchestrator.

Run on Saturday at 21:00 (launchd: com.user.daily-agent-weekly).

Stages
------
1. Collect  — general context, week tasks, all tasks, project pages (parallel)
2. Plan     — week summary LLM + per-project weekly goal LLM (run_weekly_planning)
3. Store    — save weekly plan to weekly_store
4. Deliver  — send two Telegram messages (or print in --dry-run)

CLI
---
    python pipeline/run_weekly.py                     # this week, send Telegram
    python pipeline/run_weekly.py --dry-run           # print only, no save, no send
    python pipeline/run_weekly.py --week 2026-W24     # specific ISO week

Exit codes: 0=success, 1=pipeline error, 2=delivery error
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import sys
import time
import traceback
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger
from pipeline.weekly_planner import run_weekly_planning
from pipeline.weekly_store import save_weekly_plan
from delivery.telegram_weekly import send_weekly_messages

log = get_logger("run_weekly")


def _week_start_from_iso(iso_week: str) -> datetime.date:
    """Parse '2026-W25' → Monday of that week."""
    year, wnum = iso_week.split("-W")
    return datetime.date.fromisocalendar(int(year), int(wnum), 1)


def _current_week_start() -> datetime.date:
    today = datetime.date.today()
    return today - datetime.timedelta(days=today.weekday())


# ── Stage 1: Collect (parallel) ───────────────────────────────────────────────

def _collect_parallel(week_start: datetime.date) -> dict:
    def do_general_context():
        from context.fetch_context import load_general_context
        return "general_context", load_general_context()

    def do_week_tasks():
        from collectors.collect_tasks import get_this_week_tasks
        return "week_tasks", get_this_week_tasks(week_start)

    def do_all_tasks():
        from collectors.collect_tasks import get_all_tasks
        return "all_tasks", get_all_tasks()

    def do_project_pages():
        from collectors.collect_project_pages import get_all_project_pages
        return "project_pages", get_all_project_pages()

    collectors = [do_general_context, do_week_tasks, do_all_tasks, do_project_pages]
    defaults = {
        "general_context": "",
        "week_tasks": [],
        "all_tasks": [],
        "project_pages": [],
    }

    log.info("=== Stage 1: Collect (week %s) ===", week_start)
    t0 = time.monotonic()
    data: dict = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): fn.__name__ for fn in collectors}
        for fut in concurrent.futures.as_completed(futures):
            fn_name = futures[fut]
            try:
                key, value = fut.result()
                data[key] = value
                log.info("Collected %s: %s items", key,
                         len(value) if isinstance(value, list) else f"{len(value)} chars")
            except Exception as exc:
                key = fn_name.removeprefix("do_")
                log.warning("Collector '%s' failed: %s", key, exc)
                data[key] = defaults[key]

    log.info("Collection done in %.1fs", time.monotonic() - t0)
    return data


# ── Stage 2: Plan ─────────────────────────────────────────────────────────────

def _plan(data: dict, week_start: datetime.date) -> dict:
    log.info("=== Stage 2: Plan ===")
    return run_weekly_planning(
        week_start      = week_start,
        general_context = data.get("general_context", ""),
        week_tasks      = data.get("week_tasks", []),
        all_tasks       = data.get("all_tasks", []),
        project_pages   = data.get("project_pages", []),
    )


# ── Stage 3: Store ────────────────────────────────────────────────────────────

def _store(weekly_plan: dict, week_start: datetime.date) -> None:
    log.info("=== Stage 3: Store ===")
    save_weekly_plan(weekly_plan, week_start)


# ── Stage 4: Deliver ──────────────────────────────────────────────────────────

def _deliver(weekly_plan: dict) -> bool:
    log.info("=== Stage 4: Deliver ===")
    try:
        send_weekly_messages(weekly_plan)
        log.info("Weekly Telegram delivery OK")
        return True
    except Exception as exc:
        log.error("Weekly Telegram delivery failed: %s\n%s", exc, traceback.format_exc())
        return False


def _print_result(weekly_plan: dict) -> None:
    from delivery.telegram_weekly import format_weekly_summary_message, format_weekly_plan_message
    try:
        from rich.console import Console
        from rich.rule import Rule
        console = Console()
        console.print(Rule(f"[bold green]Weekly Agent — {weekly_plan.get('week')} (dry-run)[/]"))
        console.print("[bold]── Message 1: Summary ──[/]")
        console.print(format_weekly_summary_message(weekly_plan))
        console.print(Rule())
        console.print("[bold]── Message 2: Plan ──[/]")
        console.print(format_weekly_plan_message(weekly_plan))
        console.print(Rule())
    except ImportError:
        print(format_weekly_summary_message(weekly_plan))
        print("─" * 60)
        print(format_weekly_plan_message(weekly_plan))


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    week_start: datetime.date,
    *,
    dry_run: bool = False,
) -> int:
    """
    Execute the weekly pipeline for the week starting on *week_start*.

    Returns 0=success, 1=pipeline error, 2=delivery error.
    """
    t0 = time.monotonic()
    log.info("=" * 60)
    log.info("Weekly Agent  week_start=%s  dry_run=%s", week_start, dry_run)
    log.info("=" * 60)

    try:
        data = _collect_parallel(week_start)
        weekly_plan = _plan(data, week_start)

        if dry_run:
            _print_result(weekly_plan)
            log.info("Pipeline finished in %.1fs (dry-run)", time.monotonic() - t0)
            return 0

        _store(weekly_plan, week_start)
        ok = _deliver(weekly_plan)
        exit_code = 0 if ok else 2
        log.info("Pipeline finished in %.1fs  exit_code=%d", time.monotonic() - t0, exit_code)
        return exit_code

    except Exception as exc:
        log.error("Unhandled weekly pipeline exception: %s\n%s", exc, traceback.format_exc())
        try:
            from delivery.telegram_send import send_error
            send_error(f"Weekly pipeline error: {exc}")
        except Exception:
            pass
        return 1


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Weekly Agent pipeline runner")
    parser.add_argument("--week", default=None,
                        help="ISO week to run (e.g. 2026-W25). Default: current week.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print output without saving or sending Telegram.")
    args = parser.parse_args()

    week_start = (
        _week_start_from_iso(args.week)
        if args.week
        else _current_week_start()
    )

    sys.exit(run(week_start, dry_run=args.dry_run))


if __name__ == "__main__":
    _cli()
