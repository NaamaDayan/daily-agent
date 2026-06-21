"""Shared Notion write — used by run_daily, telegram_webhook, and web server."""
from __future__ import annotations
import datetime
from utils.logger import get_logger

log = get_logger("notion_sync")


def _format_actual_text(classification: dict) -> str:
    done = [t.get("task_name", "") for t in classification.get("done") or []]
    open_ = [t.get("task_name", "") for t in classification.get("unfinished") or []]
    uncl = [
        a.get("suggested_name", "")
        for a in (
            classification.get("unclassified")
            or classification.get("unclassified_activities")
            or []
        )
    ]
    return (
        f"Done: {', '.join(done) or 'none'}\n"
        f"Open: {', '.join(open_) or 'none'}\n"
        f"Other: {', '.join(uncl[:3]) or 'none'}"
    )


def write_classification_to_notion(
    classification: dict,
    date: datetime.date,
) -> None:
    """Mark done tasks in Notion and write actual summary. All writes are best-effort."""
    from collectors.collect_tasks import mark_task_done
    from context.update_context import upsert_daily_entry

    for task in classification.get("done") or []:
        page_id = task.get("page_id")
        if not page_id:
            continue
        try:
            mark_task_done(page_id)
        except Exception as exc:
            log.warning("mark_task_done failed for %s: %s", page_id[:8], exc)

    try:
        upsert_daily_entry(date, actual=_format_actual_text(classification))
    except Exception as exc:
        log.warning("upsert_daily_entry failed for %s: %s", date, exc)
