"""
Daily pipeline orchestrator.

Called at 20:00 daily (by cron or OpenClaw).

Execution order
---------------
1. Collect  — 5 collectors run in parallel (ThreadPoolExecutor)
              typing, ActivityWatch, Cursor, Notion meetings, context
2. Summarize — two-stage Claude pipeline (Cursor pre-summary → timeline → summary)
3. Store     — save tomorrow's plan to plan_store
4. Notion    — write tomorrow's plan + today's actual (both best-effort)
5. Deliver   — send Telegram message (or print in --dry-run)
6. Log       — elapsed time, exit code

Error handling
--------------
• Individual collector failures are isolated — pipeline continues with empty data
• Summarizer hard failure (no JSON returned) → save pending/, exit 1
• Any unhandled exception → save pending/, send Telegram error, exit 1
• Delivery failure → plan already saved, exit 2

CLI
---
    python pipeline/run_daily.py                    # today, send Telegram
    python pipeline/run_daily.py --dry-run          # today, print only (no save, no send)
    python pipeline/run_daily.py --send             # explicit send (same as default)
    python pipeline/run_daily.py --date 2026-05-28  # specific date
    python pipeline/run_daily.py --date 2026-05-28 --send   # rerun + resend
    python pipeline/run_daily.py --collect-only     # run collectors, print JSON, exit

Exit codes
----------
0  success
1  pipeline error (collect/summarize failed; raw data saved to pending/)
2  delivery error (plan saved; Telegram send failed)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import pathlib
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("run_daily")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pending_dir() -> pathlib.Path:
    cfg = get_config()
    d = pathlib.Path(cfg.get("pending_dir", "~/.daily-agent/pending")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_pending(date: datetime.date, data: dict) -> pathlib.Path:
    """Persist raw collected data so a failed run can be retried manually."""
    path = _pending_dir() / f"{date}.json"
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    log.info("Raw data saved to pending: %s", path)
    return path


def _format_plan_as_bullets(plan: list[dict]) -> str:
    """
    Render a plan task list as plain bullet text for writing to Notion.

    Example:
        • [HIGH] Implement timeline builder — critical path
        • [MED] Write tests — needed before merge
    """
    lines: list[str] = []
    for t in plan:
        pri  = t.get("priority", "medium").upper()
        task = t.get("task", "")
        ctx  = t.get("context", "")
        ctx_str = f" — {ctx}" if ctx else ""
        lines.append(f"• [{pri}] {task}{ctx_str}")
    return "\n".join(lines)


# ── Stage 1: Collect (parallel) ───────────────────────────────────────────────

_COLLECTOR_DEFAULTS: dict[str, object] = {
    "typing":        [],
    "activitywatch": {},
    "cursor":        [],
    "meetings":      [],
    "context":       {"general": "", "today": None, "active_projects": [], "today_tasks": []},
}

# ── Priority mapping helpers ──────────────────────────────────────────────────

_PRIORITY_MAP = {"high": "P1 High", "medium": "P2 Medium", "low": "P3 Low"}


def _map_priority(priority_str: str) -> str:
    """Map Claude summarizer priority ('high'/'medium'/'low') → Tasks DB value."""
    return _PRIORITY_MAP.get(priority_str.lower(), "P2 Medium")


def _find_project_id_by_name(project_name: str) -> str | None:
    """Look up a project page_id by name in the active Projects DB. Returns None if not found."""
    if not project_name:
        return None
    try:
        from collectors.collect_tasks import get_active_projects
        for p in get_active_projects():
            if p["name"].strip().lower() == project_name.strip().lower():
                return p["page_id"]
    except Exception as exc:
        log.debug("_find_project_id_by_name failed for %r: %s", project_name, exc)
    return None


def _collect_parallel(date: datetime.date) -> dict:
    """
    Run all 5 collectors concurrently.

    Each collector failure is isolated: a warning is logged and the pipeline
    continues with an empty default for that data source.
    """
    # ── nested collector functions (imports inside each — fail independently) ──

    def do_typing():
        from collectors.collect_typing import load_date
        r = load_date(date)
        log.info("Typing: %d entries", len(r))
        return "typing", r

    def do_activitywatch():
        from collectors.collect_activitywatch import get_date
        r = get_date(date)
        log.info("ActivityWatch: %d active minutes",
                 r.get("total_active_minutes", 0))
        return "activitywatch", r

    def do_cursor():
        from collectors.collect_cursor import get_date
        r = get_date(date)
        log.info("Cursor: %d sessions", len(r))
        return "cursor", r

    def do_meetings():
        from collectors.collect_notion_meetings import get_date
        r = get_date(date)
        log.info("Meetings: %d", len(r))
        return "meetings", r

    def do_context():
        from context.fetch_context import load
        r = load()
        log.info("Context: general=%s, today=%s",
                 bool(r.get("general")), bool(r.get("today")))
        return "context", r

    tasks = [do_typing, do_activitywatch, do_cursor, do_meetings, do_context]

    log.info("=== Stage 1: Collect (%s) — 5 collectors in parallel ===", date)
    t0 = time.monotonic()

    data: dict = {"date": date}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): fn.__name__ for fn in tasks}
        for fut in concurrent.futures.as_completed(futures):
            fn_name = futures[fut]
            try:
                key, value = fut.result()
                data[key] = value
            except Exception as exc:
                # Map function name → data key for the default
                key = fn_name.removeprefix("do_")
                log.warning("Collector '%s' failed: %s", key, exc)
                data[key] = _COLLECTOR_DEFAULTS[key]

    log.info("Collection done in %.1fs", time.monotonic() - t0)
    return data


# ── Stage 2: Summarize ────────────────────────────────────────────────────────

def _summarize(data: dict, compress: bool = False, trace=None) -> dict:
    """
    Run the two-stage summarizer pipeline.

    Returns the structured result dict (may contain "error" key on soft failure).
    Raises ValueError on hard JSON-parse failure.
    """
    log.info("=== Stage 2: Summarize ===")
    from pipeline.summarizer import summarize as do_summarize

    result = do_summarize(
        typing_entries  = data.get("typing", []),
        activitywatch   = data.get("activitywatch") or {},
        cursor_sessions = data.get("cursor", []),
        meetings        = data.get("meetings", []),
        context         = data.get("context") or {},
        date            = data["date"],
        compress        = compress,
        trace           = trace,
    )

    if "error" in result:
        log.error("Summarizer returned error: %s", result["error"])
    else:
        log.info(
            "Summarize OK: %d done, %d unfinished, theme=%r",
            len(result.get("done", [])),
            len(result.get("unfinished", [])),
            (result.get("day_theme") or "")[:60],
        )
    return result


# ── Stage 3: Store pending summary for approval ─────────────────────────────

def _save_pending_summary(result: dict, date: datetime.date) -> None:
    """Save classification to pending file for iterative Telegram approval."""
    log.info("=== Stage 3: Save pending summary ===")
    from pipeline.pending_summary import save_pending

    classification = {
        "done": result.get("done") or [],
        "unfinished": result.get("unfinished") or [],
        "unclassified": result.get("unclassified_activities") or [],
        "day_theme": result.get("day_theme", ""),
        "time_breakdown": result.get("time_breakdown") or [],
        "unmatched_segments": result.get("unmatched_segments") or [],
    }
    try:
        save_pending(classification, date)
        log.info("Pending summary saved for %s — awaiting approval", date)
    except Exception as exc:
        log.error("save_pending failed: %s", exc)


# ── Stage 4: Store plan (legacy) ──────────────────────────────────────────────

def _store(result: dict, date: datetime.date) -> None:
    """Save tomorrow's plan to plan_store (non-fatal on failure)."""
    log.info("=== Stage 3: Store ===")
    from pipeline.plan_store import save_plan
    tomorrow = date + datetime.timedelta(days=1)
    try:
        save_plan(
            plan           = result.get("tomorrow_plan", []),
            date           = tomorrow,
            summary        = result.get("summary", ""),
            highlights     = result.get("highlights", []),
            time_breakdown = result.get("time_breakdown", []),
            blockers       = result.get("blockers", []),
            source_date    = date,
        )
        log.info("Plan stored for %s", tomorrow)
    except Exception as exc:
        log.error("plan_store.save_plan failed: %s", exc)
        # Non-fatal — continue to Notion write + delivery


# ── Stage 4: Notion writes ────────────────────────────────────────────────────

def _create_tomorrow_tasks(plan: list[dict], tomorrow: datetime.date) -> int:
    """
    Create task rows in the Tasks DB for each item in tomorrow's plan.
    Deduplicates by task name against already-existing Today tasks.
    Returns count of tasks created.
    """
    from collectors.collect_tasks import create_task, get_today_tasks

    try:
        existing = get_today_tasks(tomorrow)
        existing_names = {t["task_name"].strip().lower() for t in existing}
    except Exception:
        existing_names = set()

    created = 0
    for item in plan:
        task_name = item.get("task", "").strip()
        if not task_name:
            continue
        if task_name.lower() in existing_names:
            log.debug("Skipping duplicate task for %s: %r", tomorrow, task_name)
            continue
        try:
            create_task(
                task_name       = task_name,
                project_page_id = _find_project_id_by_name(item.get("project", "")),
                status          = "🎯 Today",
                priority        = _map_priority(item.get("priority", "medium")),
                scheduled_date  = tomorrow,
                notes           = item.get("context", ""),
            )
            existing_names.add(task_name.lower())   # prevent duplicates within this run
            created += 1
        except Exception as exc:
            log.warning("create_task failed for %r: %s", task_name[:60], exc)

    return created


def _write_notion(result: dict, date: datetime.date, data: dict | None = None) -> None:
    """
    Write to Notion (all writes are best-effort — failures do NOT abort delivery).

    New behaviour (Task 5C):
      1. Tomorrow's plan  → CREATE task rows in Tasks DB for each tomorrow_plan item
                            (dedup by name; still write bullet fallback to daily page)
      2. Today's actual   → upsert_daily_entry(date, actual=summary)
                            with optional task completion count
    """
    from context.update_context import upsert_daily_entry

    tomorrow = date + datetime.timedelta(days=1)
    plan     = result.get("tomorrow_plan", [])

    # ── Create task rows for tomorrow ─────────────────────────────────────────
    if plan:
        try:
            created = _create_tomorrow_tasks(plan, tomorrow)
            log.info("Tasks DB: created %d task(s) for %s", created, tomorrow)
        except Exception as exc:
            log.warning("Tasks DB write failed for %s: %s", tomorrow, exc)
            # Fallback: write bullet plan to the Notion page
            try:
                upsert_daily_entry(tomorrow, plan=_format_plan_as_bullets(plan))
                log.info("Fallback: wrote plan text to Notion page for %s", tomorrow)
            except Exception as exc2:
                log.warning("Notion fallback plan write failed: %s", exc2)

    # ── Write today's actual + optional task completion count ─────────────────
    # Deferred until user approves via Telegram (pending_summary flow).
    log.info("Notion actual write deferred until summary approval")


# ── Task checklist formatter (Task 5A) ───────────────────────────────────────

def _format_task_checklist(today_tasks: list[dict]) -> str:
    """
    Format today's tasks as a numbered checklist appended to the Telegram message.

    Example:
        📋 *Today's tasks — mark as done?*
        1. [P1 High] Build timeline module — daily_agent (est. 60 min)
        2. [P2 Medium] Write tests
        Reply: "1 2 done, 3 defer" or "all done" or "add: new task as P1"
    """
    if not today_tasks:
        return ""
    lines = ["", "📋 *Today's tasks — mark as done?*"]
    for i, t in enumerate(today_tasks, 1):
        pri  = t.get("priority", "")
        name = t.get("task_name", "")
        proj = t.get("project", "")
        est  = t.get("estimated_minutes")
        parts = [f"[{pri}] {name}"]
        if proj:
            parts.append(f"— {proj}")
        if est:
            parts.append(f"(est. {est} min)")
        lines.append(f"{i}. {' '.join(parts)}")
    lines.append('_Reply: "1 2 done, 3 defer" or "all done" or "add: new task as P1"_')
    return "\n".join(lines)


# ── Stage 6: Daily Plan ───────────────────────────────────────────────────────

def _plan_tomorrow(
    result: dict,
    date: datetime.date,
    data: dict,
    *,
    dry_run: bool,
) -> None:
    """
    Stage 6: generate per-project tasks for tomorrow and send as a second Telegram message.

    Non-fatal — exceptions are logged but do not change the pipeline exit code.
    Runs in both live and dry-run modes.
    """
    import os
    log.info("=== Stage 6: Daily Plan ===")

    from pipeline.weekly_store import get_current_weekly_plan
    from delivery.telegram_daily_plan import (
        NO_WEEKLY_PLAN_MSG, format_daily_plan_message, send_daily_plan,
    )

    tomorrow = date + datetime.timedelta(days=1)
    detail = os.environ.get("DAILY_PLAN_DETAIL", "medium").lower()

    # 6A: load weekly plan
    weekly_plan = get_current_weekly_plan()
    if weekly_plan is None:
        log.warning("Stage 6: no weekly plan found — sending fallback message")
        if dry_run:
            print(NO_WEEKLY_PLAN_MSG)
        else:
            from delivery.telegram_send import send_text
            send_text(NO_WEEKLY_PLAN_MSG)
        return

    # 6B: collect project pages
    try:
        from collectors.collect_project_pages import get_all_project_pages
        project_pages = get_all_project_pages()
    except Exception as exc:
        log.warning("Stage 6: get_all_project_pages failed: %s", exc)
        project_pages = []

    # 6C: collect all tasks
    try:
        from collectors.collect_tasks import get_all_tasks
        all_tasks = get_all_tasks()
    except Exception as exc:
        log.warning("Stage 6: get_all_tasks failed: %s", exc)
        all_tasks = []

    # 6D: collect calendar
    try:
        from collectors.collect_calendar import get_tomorrow_events
        calendar_events = get_tomorrow_events()
    except Exception as exc:
        log.warning("Stage 6: get_tomorrow_events failed: %s", exc)
        calendar_events = []

    general_context = (data.get("context") or {}).get("general", "")

    # 6E: per-project LLM calls
    from pipeline.daily_planner import plan_tomorrow
    plan = plan_tomorrow(
        projects=project_pages,
        all_tasks=all_tasks,
        weekly_plan=weekly_plan,
        today_result=result,
        calendar_events=calendar_events,
        general_context=general_context,
        detail=detail,
    )
    log.info("Stage 6: %d project(s) planned for %s", len(plan), tomorrow)

    # 6F: send / print
    if dry_run:
        formatted = format_daily_plan_message(plan, tomorrow)
        try:
            from rich.console import Console
            Console().print(formatted)
        except ImportError:
            print(formatted)
    else:
        send_daily_plan(plan, tomorrow)


# ── Stage 5: Deliver ──────────────────────────────────────────────────────────

def _deliver(result: dict, date: datetime.date) -> bool:
    """Format and send the Telegram summary. Returns True on success."""
    log.info("=== Stage 5: Deliver ===")
    try:
        from delivery.telegram_send import send_classification
        footer = "\n\n_Reply to edit items, or say *approve* to save to Notion._"
        send_classification(result, date, footer=footer)
        log.info("Telegram delivery OK")
        return True
    except Exception as exc:
        log.error("Telegram delivery failed: %s\n%s", exc, traceback.format_exc())
        return False


def _print_result(result: dict, date: datetime.date) -> None:
    """Print the formatted Telegram message to stdout (dry-run mode)."""
    from delivery.telegram_send import format_classification_message
    formatted = format_classification_message(result, date)
    formatted += "\n\n_Reply to edit items, or say *approve* to save to Notion._"
    # Append task checklist
    today_tasks = result.get("today_tasks", [])
    if today_tasks:
        formatted += _format_task_checklist(today_tasks)
    try:
        from rich.console import Console
        from rich.rule import Rule
        console = Console()
        console.print(Rule(f"[bold green]Daily Agent — {date}  (dry-run)[/]"))
        console.print(formatted)
        console.print(Rule())
        console.print(
            f"[dim]{len(formatted)} chars  |  "
            f"{len(result.get('done', []))} done  |  "
            f"{len(result.get('unfinished', []))} unfinished  |  "
            f"{len(result.get('unclassified_activities', []))} unclassified[/]"
        )
    except ImportError:
        print(f"{'─' * 60}")
        print(formatted)
        print(f"{'─' * 60}")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(
    date: datetime.date,
    *,
    dry_run: bool = False,
    skip_notion_write: bool = False,
    compress: bool = False,
    enable_trace: bool | None = None,
) -> int:
    """
    Execute the full daily pipeline for *date*.

    Parameters
    ----------
    date              : The date being summarised (usually today).
    dry_run           : If True — collect + summarise but do NOT save plan,
                        write Notion, or send Telegram. Print result instead.
    skip_notion_write : If True — skip Notion writes (plan + actual).
    compress          : Enable second-pass compression before Stage 2.
                        See summarizer.build_prompt for full description.
    enable_trace      : If False, skip debug trace file. None = use config.

    Returns exit code: 0 = success, 1 = pipeline error, 2 = delivery error.
    """
    from pipeline.daily_trace import DailyPipelineTrace

    t_pipeline = time.monotonic()
    trace = DailyPipelineTrace.for_run(
        date, dry_run=dry_run, enabled=enable_trace,
    )
    log.info("=" * 60)
    log.info("Daily Agent pipeline  date=%s  dry_run=%s  compress=%s  trace=%s",
             date, dry_run, compress, trace is not None)
    log.info("=" * 60)

    data: dict | None = None
    exit_code = 1

    try:
        # ── 1. Collect ────────────────────────────────────────────────────────
        data = _collect_parallel(date)
        if trace:
            trace.add_collect(data)

        # ── 2. Summarize ──────────────────────────────────────────────────────
        try:
            result = _summarize(data, compress=compress, trace=trace)
        except ValueError as exc:
            # Hard failure: Claude returned un-parseable JSON
            log.error("Summarizer hard failure: %s", exc)
            _save_pending(date, {"collected": data, "error": str(exc)})
            if trace:
                trace.set_run_meta(error=str(exc))
            try:
                from delivery.telegram_send import send_error
                send_error(f"Summarizer failed: {exc}")
            except Exception:
                pass
            exit_code = 1
            return exit_code

        # Carry today_tasks forward into result for delivery / print
        result.setdefault(
            "today_tasks",
            (data.get("context") or {}).get("today_tasks") or [],
        )

        if "error" in result and not result.get("day_theme") and not result.get("summary"):
            # Soft failure with no usable output
            _save_pending(date, {"collected": data, "error": result.get("error")})
            log.error("Pipeline aborted — raw data saved for retry")
            if trace:
                trace.set_run_meta(error=result.get("error", "unknown"))
            try:
                from delivery.telegram_send import send_error
                send_error(f"Pipeline error: {result.get('error')}")
            except Exception:
                pass
            exit_code = 1
            return exit_code

        if trace:
            trace.add_final_result(result)

        # ── 3. Store pending summary for approval ─────────────────────────────
        if not dry_run:
            _save_pending_summary(result, date)
            _store(result, date)

        # ── 4. Notion writes (best-effort) ────────────────────────────────────
        if not dry_run and not skip_notion_write:
            log.info("=== Stage 4: Notion writes ===")
            _write_notion(result, date, data=data)

        # ── 5. Deliver ────────────────────────────────────────────────────────
        from delivery.telegram_send import format_classification_message
        telegram_msg = format_classification_message(result, date)
        telegram_msg += "\n\n_Reply to edit items, or say *approve* to save to Notion._"
        if trace:
            trace.add_telegram_preview(telegram_msg)

        if dry_run:
            log.info("=== Stage 5: Deliver (dry-run — printing to stdout) ===")
            _print_result(result, date)
            exit_code = 0
        else:
            ok = _deliver(result, date)
            exit_code = 0 if ok else 2

        # ── 6. Daily Plan ─────────────────────────────────────────────────────
        try:
            _plan_tomorrow(result, date, data, dry_run=dry_run)
        except Exception as exc:
            log.error("Stage 6 failed: %s", exc)
            # Non-fatal — exit_code unchanged

        elapsed = time.monotonic() - t_pipeline
        log.info("Pipeline finished in %.1fs  exit_code=%d", elapsed, exit_code)
        return exit_code

    except Exception as exc:
        # Catch-all: unhandled exception anywhere in the pipeline
        log.error(
            "Unhandled pipeline exception: %s\n%s", exc, traceback.format_exc()
        )
        payload = {"error": str(exc), "traceback": traceback.format_exc()}
        if data is not None:
            payload["collected"] = data
        _save_pending(date, payload)
        if trace:
            trace.set_run_meta(error=traceback.format_exc())
        try:
            from delivery.telegram_send import send_error
            send_error(f"Unhandled error: {exc}")
        except Exception:
            pass
        exit_code = 1
        return exit_code

    finally:
        if trace:
            elapsed = time.monotonic() - t_pipeline
            trace.set_run_meta(exit_code=exit_code, elapsed_s=elapsed)
            trace.write()


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Daily Agent pipeline runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--date", default="today",
        help="Date to run for (YYYY-MM-DD or 'today'). Default: today",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Collect + summarize but do NOT save plan, write Notion, "
            "or send Telegram. Print the formatted message instead."
        ),
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Explicitly send via Telegram (same as default; provided for clarity)",
    )
    parser.add_argument(
        "--skip-notion-write", action="store_true",
        help="Skip Notion daily-entry writes (plan + actual)",
    )
    parser.add_argument(
        "--no-trace", action="store_true",
        help="Skip writing summarization_prompts/YYYY-MM-DD.md debug trace",
    )
    parser.add_argument(
        "--compress", action="store_true",
        help=(
            "Enable second-pass compression before Stage 2. "
            "When the assembled synthesis prompt exceeds "
            "config['max_synthesis_tokens'] (default 12 000 tokens), "
            "the day-timeline, meetings, and context sections are each "
            "compressed to ≤3 bullet points via a cheap haiku call. "
            "Use with --dry-run to compare output quality against the baseline."
        ),
    )
    parser.add_argument(
        "--collect-only", action="store_true",
        help="Run collectors only, print JSON to stdout, and exit",
    )
    parser.add_argument(
        "--show-prompt", action="store_true",
        help="With --dry-run: print the classifier prompt (including few-shot) and exit",
    )
    parser.add_argument(
        "--show-costs", action="store_true",
        help="Print a cost summary from the JSONL log and exit (no pipeline run)",
    )
    parser.add_argument(
        "--days", type=int, default=30, metavar="N",
        help="Number of days to include in --show-costs (default: 30)",
    )
    args = parser.parse_args()

    # ── Cost summary ──────────────────────────────────────────────────────────
    if args.show_costs:
        from utils.cost_logger import print_cost_summary
        print_cost_summary(days=args.days)
        return

    date = (
        datetime.date.today()
        if args.date == "today"
        else datetime.date.fromisoformat(args.date)
    )

    if args.collect_only:
        data = _collect_parallel(date)
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return

    if args.show_prompt:
        data = _collect_parallel(date)
        from pipeline.summarizer import build_timeline, presummary_all_cursor
        from pipeline.classifier import build_classification_prompt
        from collectors.collect_tasks import get_classifiable_tasks

        cursor_presummaries = presummary_all_cursor(data.get("cursor") or [])
        timeline = build_timeline(
            data.get("typing") or [],
            data.get("activitywatch") or {},
            cursor_presummaries,
        )
        tasks = get_classifiable_tasks(date)
        if not tasks:
            tasks = (data.get("context") or {}).get("today_tasks") or []
        prompt = build_classification_prompt(
            timeline,
            tasks,
            data.get("meetings") or [],
            active_projects=(data.get("context") or {}).get("active_projects") or [],
        )
        print(prompt)
        return

    exit_code = run(
        date,
        dry_run           = args.dry_run,
        skip_notion_write = args.skip_notion_write,
        compress          = args.compress,
        enable_trace      = False if args.no_trace else None,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    _cli()
