"""
Telegram webhook — reply handler for plan edits and context updates.

Design
------
handle_reply() is the primary public function. It is called by OpenClaw
(the orchestrator) whenever the user replies to a Telegram message. It is a
pure function — it detects intent, applies side effects (plan edit, Notion
update), sends the confirmation via Telegram, and returns a status string.

Intent detection (keyword-based, ordered by priority)
------------------------------------------------------
context_update  Message starts with "Update context:" or "Context:"
show_plan       Contains "show plan", "what's the plan", "plan?", or "/plan"
manual_run      Contains "run now", "trigger", or starts with "/run"
status          Contains "status" or starts with "/status"
help            Starts with "/help" or equals "help"
plan_edit       Contains any edit keyword (see _PLAN_EDIT_KEYWORDS)
unknown         None of the above → return help hint

Side effects per intent
-----------------------
plan_edit       plan_store.update_plan(text, tomorrow)
                → send_text("✓ Plan updated\n\n" + format_plan_for_telegram(plan))
                → return "✓ Plan updated"

context_update  update_general_context(content_after_prefix)
                → return "✓ Context updated in Notion."

show_plan       load_plan(tomorrow) → format_plan_for_telegram(plan)
                → send_text(plan_str)
                → return plan_str

Optional server modes (for running as a persistent listener)
------------------------------------------------------------
run_poll()      Long-poll Telegram getUpdates loop (dev mode)
run_webhook()   Minimal HTTP server receiving Telegram POSTs (production mode)
Both delegate to handle_reply() for each incoming message.

CLI
---
    python delivery/telegram_webhook.py --message "move 3 to next week"
    python delivery/telegram_webhook.py --poll
    python delivery/telegram_webhook.py --webhook [--host HOST] [--port PORT]
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import requests

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("telegram_webhook")


_APPROVAL_PHRASES = frozenset({
    "approve", "approved", "ok", "looks good", "good", "yes", "done",
    "lgtm", "confirm", "perfect", "great", "correct", "👍",
})


def _is_final_approval(text: str) -> bool:
    """Exact match only — prevents 'Task 3 is done' from triggering approval."""
    return text.strip().lower() in _APPROVAL_PHRASES


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



def _classification_for_telegram(
    classification: dict,
    pending: dict | None = None,
) -> dict:
    """Build result dict for format_classification_message."""
    current = classification
    if pending and not classification.get("day_theme"):
        current = {**pending.get("current", {}), **classification}
    return {
        **current,
        "done": current.get("done") or [],
        "unfinished": current.get("unfinished") or [],
        "unclassified_activities": (
            current.get("unclassified")
            or current.get("unclassified_activities")
            or []
        ),
        "day_theme": current.get("day_theme", ""),
        "time_breakdown": current.get("time_breakdown") or [],
    }


def _apply_edit_and_show(
    pending: dict,
    instruction: str,
    date: datetime.date,
) -> str:
    from pipeline import pending_summary
    from delivery.telegram_send import format_classification_message, send_classification
    from pipeline.notion_sync import write_classification_to_notion

    updated = pending_summary.apply_edit(instruction, date)
    record = pending_summary.load_pending(date) or pending
    version = record.get("version", 1)
    payload = _classification_for_telegram(updated, record)
    # Sync to Notion after each edit
    try:
        write_classification_to_notion(updated, date)
    except Exception as exc:
        log.warning("Notion sync after edit failed: %s", exc)
    footer = (
        f"\n\n_Edit applied \\(v{version}\\)\\. Reply to keep editing\\._"
    )
    try:
        send_classification(payload, date, footer=footer)
    except Exception as exc:
        log.warning("Failed to send edit confirmation: %s", exc)
    formatted = format_classification_message(payload, date) + footer
    return formatted


def _execute_approval(pending: dict, date: datetime.date) -> str:
    from pipeline import pending_summary
    from delivery.telegram_send import send_text
    from config_loader import get_config
    from pipeline.notion_sync import write_classification_to_notion

    approved = pending_summary.save_reviewed(date)
    current = approved.get("current") or {}

    cfg = get_config()
    if cfg.get("learning_enabled", True):
        from pipeline import learning_store
        learning_store.record_approved_day(
            approved_classification=current,
            date=date,
        )

    try:
        write_classification_to_notion(current, date)
    except Exception as exc:
        log.warning("Notion write on approval failed: %s", exc)

    done_names = [t.get("task_name", "") for t in current.get("done") or []]
    open_names = [t.get("task_name", "") for t in current.get("unfinished") or []]
    reply = (
        f"✅ Approved and saved (v{approved.get('version', 1)}).\n"
        f"• Done: {', '.join(done_names) or 'none'}\n"
        f"• Open: {', '.join(open_names) or 'none'}"
    )
    try:
        send_text(reply)
    except Exception as exc:
        log.warning("Failed to send approval confirmation: %s", exc)
    return reply


def handle_approval_reply(
    message_text: str,
    date: datetime.date | None = None,
) -> str:
    """
    Handle iterative summary approval: apply edit or finalize approval.

    Returns a human-readable status string (also sent to Telegram when possible).
    """
    from pipeline import pending_summary

    if date is None:
        date = pending_summary.find_active_pending_date()

    if date is None:
        return "No pending summary found for today. Run /summary to regenerate."

    pending = pending_summary.load_pending(date)
    if pending is None:
        return "No pending summary found for today. Run /summary to regenerate."

    if pending.get("status") in ("approved", "reviewed"):
        return "Summary already approved for this date."

    if pending_summary.is_expired(date):
        return "Pending summary expired (>36h). Run the daily pipeline to regenerate."

    if _is_final_approval(message_text):
        return _execute_approval(pending, date)

    return _apply_edit_and_show(pending, message_text, date)


def has_pending() -> bool:
    """Return True if an active (non-approved, non-expired) pending summary exists."""
    from pipeline import pending_summary
    return pending_summary.has_active_pending()


# ── Intent detection ──────────────────────────────────────────────────────────

_PLAN_EDIT_KEYWORDS = [
    "move", "add", "remove", "replace", "swap", "change", "delete",
    "postpone", "skip", "insert", "put", "shift", "reschedule",
    "next week", "tomorrow", "priority", "drop", "push", "pull",
    "make item",
]

# Task-update keywords — checked BEFORE plan-edit
_TASK_UPDATE_KEYWORDS = [
    "done", "defer", "deferred", "all done", "completed",
    "add task", "add:", "postpone",
]

_CONTEXT_PREFIXES  = ("update context:", "context:")
_SHOW_PLAN_TOKENS  = ("show plan", "what's the plan", "plan?", "/plan")
_MANUAL_RUN_TOKENS = ("run now", "trigger", "/run")
_STATUS_TOKENS     = ("status", "/status")
_HELP_TOKENS       = ("/help", "help")

import re as _re
_DIGIT_DONE_RE = _re.compile(r'\b\d[\d\s,]*\s+done\b', _re.IGNORECASE)


def _detect_intent(text: str) -> str:
    """
    Return one of:
        task_update | context_update | show_plan | manual_run |
        status | help | plan_edit | unknown

    task_update is checked BEFORE plan_edit (claw.md requirement).
    """
    t = text.strip().lower()

    # Context update — check first (prefix guard against "add context" hitting plan_edit)
    if t.startswith(_CONTEXT_PREFIXES):
        return "context_update"

    # Task update — "done", "defer", "all done", digits-done patterns
    if any(kw in t for kw in _TASK_UPDATE_KEYWORDS):
        return "task_update"
    if _DIGIT_DONE_RE.search(t):
        return "task_update"

    # Show plan
    if any(tok in t for tok in _SHOW_PLAN_TOKENS):
        return "show_plan"

    # Manual run
    if any(tok in t for tok in _MANUAL_RUN_TOKENS):
        return "manual_run"

    # Status
    if any(tok in t for tok in _STATUS_TOKENS):
        return "status"

    # Help
    if t in _HELP_TOKENS or t.startswith("/help"):
        return "help"

    # Plan edit — check for any edit keyword
    if any(kw in t for kw in _PLAN_EDIT_KEYWORDS):
        return "plan_edit"

    return "unknown"


def _extract_context_content(text: str) -> str:
    """Strip the 'Update context:' / 'Context:' prefix and return the payload."""
    stripped = text.strip()
    for prefix in ("Update context:", "Context:"):
        if stripped.lower().startswith(prefix.lower()):
            return stripped[len(prefix):].strip()
    return stripped


# ── Date helpers ──────────────────────────────────────────────────────────────

def _tomorrow() -> datetime.date:
    return datetime.date.today() + datetime.timedelta(days=1)


# ── Intent handlers ───────────────────────────────────────────────────────────

def _handle_task_update(text: str, today_tasks: list[dict] | None = None) -> str:
    """
    Parse and apply an EOD task-update reply (done / defer / add).

    Loads today's tasks from the DB if not provided. Sends a formatted
    confirmation to Telegram and returns the status string.
    """
    from context.update_context import process_eod_task_replies
    from delivery.telegram_send import send_text
    from collectors.collect_tasks import get_today_tasks

    if today_tasks is None:
        try:
            today_tasks = get_today_tasks()
        except Exception as exc:
            log.error("Failed to load today_tasks for EOD reply: %s", exc)
            reply = f"Could not load today's tasks: {exc}"
            try:
                send_text(reply)
            except Exception:
                pass
            return reply

    if not today_tasks:
        reply = (
            "No tasks found for today in the Tasks DB. "
            "Add tasks via Notion or wait for the next daily run."
        )
        try:
            send_text(reply)
        except Exception:
            pass
        return reply

    log.info("EOD task update: %r (tasks=%d)", text[:80], len(today_tasks))
    try:
        summary = process_eod_task_replies(text, today_tasks)
    except Exception as exc:
        log.error("process_eod_task_replies failed: %s", exc)
        reply = f"Sorry, couldn't process that update: {exc}"
        try:
            send_text(reply)
        except Exception:
            pass
        return reply

    # Build confirmation message
    parts: list[str] = []
    if summary["done_count"]:
        names = ", ".join(summary["done_names"][:5])
        parts.append(f"✅ {summary['done_count']} done: {names}")
    if summary["deferred_count"]:
        names = ", ".join(summary["deferred_names"][:5])
        parts.append(f"⏸ {summary['deferred_count']} deferred to tomorrow: {names}")
    if summary["created_count"]:
        parts.append(f"➕ {summary['created_count']} new task(s) added")
    if not parts:
        parts.append("No tasks were updated (check the reply format).")

    confirmation = "\n".join(parts)
    try:
        send_text(confirmation)
    except Exception as exc:
        log.warning("Failed to send EOD confirmation: %s", exc)

    return confirmation


def _handle_plan_edit(text: str) -> str:
    from pipeline.plan_store import update_plan, format_plan_for_telegram
    from delivery.telegram_send import send_text

    tomorrow = _tomorrow()
    log.info("Plan edit for %s: %r", tomorrow, text[:80])

    try:
        updated = update_plan(text, tomorrow)
    except Exception as exc:
        log.error("update_plan failed: %s", exc)
        reply = f"Sorry, I couldn't apply that edit: {exc}"
        try:
            send_text(reply)
        except Exception:
            pass
        return reply

    plan_str = format_plan_for_telegram(updated)
    confirmation = f"✓ Plan updated\n\n{plan_str}"
    try:
        send_text(confirmation)
    except Exception as exc:
        log.warning("Failed to send plan confirmation: %s", exc)

    return "✓ Plan updated"


def _handle_context_update(text: str) -> str:
    from context.update_context import update_general_context

    content = _extract_context_content(text)
    if not content:
        return "Nothing to update — please provide context text after 'Context:'."

    log.info("Updating general context (%d chars)", len(content))
    try:
        update_general_context(content)
    except Exception as exc:
        log.error("update_general_context failed: %s", exc)
        return f"Failed to update Notion context: {exc}"

    return "✓ Context updated in Notion."


def _handle_show_plan() -> str:
    from pipeline.plan_store import load_plan, format_plan_for_telegram
    from delivery.telegram_send import send_text

    tomorrow = _tomorrow()
    plan = load_plan(tomorrow)
    if not plan:
        reply = f"No plan found for {tomorrow}. Run the daily pipeline first."
        try:
            send_text(reply)
        except Exception:
            pass
        return reply

    plan_str = format_plan_for_telegram(plan)
    header   = f"📅 Plan for {tomorrow}\n\n"
    full     = header + plan_str
    try:
        send_text(full)
    except Exception as exc:
        log.warning("Failed to send plan: %s", exc)

    return full


def _handle_status() -> str:
    from pipeline.plan_store import load_plan

    tomorrow = _tomorrow()
    plan     = load_plan(tomorrow)
    total    = len(plan)
    done     = sum(1 for t in plan if t.get("done"))
    reply    = f"📊 Status: {done}/{total} tasks done for {tomorrow}"
    try:
        from delivery.telegram_send import send_text
        send_text(reply)
    except Exception:
        pass
    return reply


_HELP_TEXT = (
    "Daily Agent — reply commands:\n\n"
    "• Edit the plan: \"Move 3 to next week\", \"Add: X as priority 1\", "
    "\"Replace 2 with: Y\"\n"
    "• Show plan: \"show plan\" or /plan\n"
    "• Update context: \"Context: I'm now focused on Z\"\n"
    "• Status: \"status\" or /status\n"
    "• Help: /help"
)


def _handle_help() -> str:
    try:
        from delivery.telegram_send import send_text
        send_text(_HELP_TEXT)
    except Exception:
        pass
    return _HELP_TEXT


_UNKNOWN_HINT = (
    "Didn't understand that. Try:\n"
    "• \"Move 3 to next week\"\n"
    "• \"Add: review investor deck as priority 1\"\n"
    "• \"show plan\"\n"
    "• /help for all commands"
)


# ── Main public function ──────────────────────────────────────────────────────

def handle_reply(message_text: str) -> str:
    """
    Process an incoming Telegram reply.

    Called by OpenClaw (or the poll/webhook loops below).
    Detects intent, applies side effects, sends Telegram confirmation,
    and returns a short status string for the caller.

    Parameters
    ----------
    message_text : str
        The raw text of the user's Telegram reply.

    Returns
    -------
    str
        Human-readable status: "✓ Plan updated", "✓ Context updated in Notion.",
        the plan text (for show_plan), an error message, or the help string.
    """
    text = message_text.strip()

    # Review mode — pending summary takes priority over all other intents
    if not text.startswith("/") and has_pending():
        return handle_approval_reply(text)

    intent = _detect_intent(text)
    log.info("handle_reply: intent=%s  text=%r", intent, text[:80])

    if intent == "task_update":
        return _handle_task_update(text)

    if intent == "plan_edit":
        return _handle_plan_edit(text)

    if intent == "context_update":
        return _handle_context_update(text)

    if intent == "show_plan":
        return _handle_show_plan()

    if intent == "manual_run":
        # OpenClaw triggers the pipeline; we just acknowledge here
        return "Triggering daily run is handled by OpenClaw. Use the /run claw command."

    if intent == "status":
        return _handle_status()

    if intent == "help":
        return _handle_help()

    # Unknown — send hint
    try:
        from delivery.telegram_send import send_text
        send_text(_UNKNOWN_HINT)
    except Exception:
        pass
    return _UNKNOWN_HINT


# ── Long-poll loop ────────────────────────────────────────────────────────────

def _bot_request(method: str, **kwargs) -> dict:
    cfg   = get_config()
    token = cfg["telegram_bot_token"]
    url   = f"https://api.telegram.org/bot{token}/{method}"
    resp  = requests.post(url, json=kwargs, timeout=30)
    resp.raise_for_status()
    data  = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data}")
    return data


def _get_updates(offset: int | None = None) -> list[dict]:
    """Long-poll for updates (blocks up to 20 seconds)."""
    kwargs: dict = {"timeout": 20, "allowed_updates": ["message"]}
    if offset is not None:
        kwargs["offset"] = offset
    return _bot_request("getUpdates", **kwargs).get("result", [])


def run_poll(*, max_iterations: int | None = None) -> None:
    """
    Poll Telegram for updates and call handle_reply() for each message.

    *max_iterations* limits the loop (None = run forever).
    Useful for local development — no public HTTPS endpoint needed.
    """
    log.info("Starting long-poll mode (Ctrl-C to stop)…")
    offset: int | None = None
    iterations = 0

    while True:
        if max_iterations is not None and iterations >= max_iterations:
            break
        try:
            updates = _get_updates(offset)
        except KeyboardInterrupt:
            log.info("Long-poll stopped by user")
            break
        except Exception as exc:
            log.warning("getUpdates failed: %s — retrying in 5s", exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = update.get("update_id", 0) + 1
            msg    = update.get("message", {})
            text   = msg.get("text", "").strip()
            if text:
                try:
                    handle_reply(text)
                except Exception as exc:
                    log.error("handle_reply error: %s", exc)

        iterations += 1


# ── Webhook server ────────────────────────────────────────────────────────────

def run_webhook(host: str = "0.0.0.0", port: int = 8443) -> None:
    """
    Start a minimal HTTP server to receive Telegram webhook POSTs.

    In production, place behind an HTTPS reverse proxy (nginx / caddy)
    or use ngrok for a public URL.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                update = json.loads(body)
                msg    = update.get("message", {})
                text   = msg.get("text", "").strip()
                if text:
                    handle_reply(text)
            except Exception as exc:
                log.error("Webhook handler error: %s", exc)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
            log.debug(fmt, *args)

    server = HTTPServer((host, port), _Handler)
    log.info("Webhook server listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Webhook server stopped")
        server.shutdown()


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Telegram reply handler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python delivery/telegram_webhook.py --message 'move 3 to next week'\n"
            "  python delivery/telegram_webhook.py --message 'show plan'\n"
            "  python delivery/telegram_webhook.py --poll\n"
            "  python delivery/telegram_webhook.py --webhook --port 8443\n"
        ),
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--approve", metavar="TEXT",
        help="Handle a summary approval reply (edit or final approve)",
    )
    mode.add_argument(
        "--has-pending", action="store_true",
        help="Exit 0 if an active pending summary exists, else exit 1",
    )
    mode.add_argument(
        "--message", metavar="TEXT",
        help="Simulate an incoming reply and print the result (no Telegram needed)",
    )
    mode.add_argument(
        "--task-update", metavar="TEXT",
        help=(
            "Process an EOD task-update reply directly. "
            "Loads today's tasks from the Tasks DB, parses the instruction, "
            "applies done/defer/create operations, and prints the confirmation."
        ),
    )
    mode.add_argument(
        "--poll", action="store_true",
        help="Long-poll mode — poll Telegram for updates (dev/local)",
    )
    mode.add_argument(
        "--webhook", action="store_true",
        help="HTTP webhook server mode",
    )

    parser.add_argument(
        "--date", default="today",
        help="Date for --approve (YYYY-MM-DD or 'today')",
    )
    parser.add_argument("--host",  default="0.0.0.0", help="Webhook bind host")
    parser.add_argument("--port",  type=int, default=8443, help="Webhook port")
    parser.add_argument(
        "--no-send", action="store_true",
        help="With --message: detect intent only, do NOT send Telegram",
    )

    args = parser.parse_args()

    date = (
        datetime.date.today()
        if args.date == "today"
        else datetime.date.fromisoformat(args.date)
    )

    if args.has_pending:
        sys.exit(0 if has_pending() else 1)

    if args.approve is not None:
        result = handle_approval_reply(args.approve, date=date)
        print(result)

    elif args.message:
        if args.no_send:
            intent = _detect_intent(args.message)
            print(f"Intent: {intent}")
            print(f"Text:   {args.message!r}")
        else:
            result = handle_reply(args.message)
            print(result)

    elif args.task_update:
        result = _handle_task_update(args.task_update)
        print(result)

    elif args.poll:
        run_poll()

    elif args.webhook:
        run_webhook(host=args.host, port=args.port)


if __name__ == "__main__":
    _cli()
