"""
Pending daily summary — iterative approval storage and edit application.

Files: config["plans_dir"]/pending-YYYY-MM-DD.json

Public API
----------
save_pending(classification, date=None) -> None
load_pending(date=None) -> dict | None
get_current(date=None) -> dict | None
apply_edit(edit_instruction, date=None) -> dict
save_approved(date=None) -> dict
is_expired(date=None) -> bool
has_active_pending() -> bool
"""

from __future__ import annotations

import copy
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import anthropic

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("pending_summary")

_EXPIRY_HOURS = 36

_EDIT_SYSTEM = (
    "You apply user edits to a structured daily summary JSON.\n"
    "The summary has three sections: 'done', 'unfinished', 'unclassified'.\n"
    "Items are referenced by their display number (done items first, then\n"
    "unfinished, then unclassified, numbered continuously from 1).\n"
    "Apply the instruction literally. If the instruction is ambiguous,\n"
    "apply the most conservative interpretation.\n"
    "Return ONLY the updated JSON. Same schema. No explanation."
)


def _plans_dir() -> pathlib.Path:
    cfg = get_config()
    d = pathlib.Path(cfg.get("plans_dir", "~/.daily-agent/plans")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_date(date: datetime.date | None) -> datetime.date:
    return date or datetime.date.today()


def _pending_path(date: datetime.date) -> pathlib.Path:
    return _plans_dir() / f"pending-{date.isoformat()}.json"


def _now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds")


def _normalize_classification(data: dict) -> dict:
    """Ensure consistent done / unfinished / unclassified keys."""
    out: dict = {
        "done": list(data.get("done") or []),
        "unfinished": list(data.get("unfinished") or []),
        "unclassified": list(
            data.get("unclassified")
            or data.get("unclassified_activities")
            or []
        ),
    }
    for key in ("day_theme", "time_breakdown", "unmatched_segments"):
        if key in data:
            out[key] = data[key]
    return out


def _read_file(date: datetime.date) -> dict | None:
    path = _pending_path(date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to read pending summary %s: %s", path, exc)
        return None


def _write_file(record: dict, date: datetime.date) -> None:
    path = _pending_path(date)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def save_pending(classification: dict, date: datetime.date | None = None) -> None:
    """Create pending file. version=1, status=awaiting_approval."""
    date = _resolve_date(date)
    record = {
        "date": date.isoformat(),
        "status": "active",
        "created_at": _now_iso(),
        "approved_at": None,
        "version": 1,
        "current": _normalize_classification(classification),
        "history": [],
    }
    _write_file(record, date)
    log.info("Pending summary saved for %s (v1)", date)


def load_pending(date: datetime.date | None = None) -> dict | None:
    """Return full pending record or None."""
    return _read_file(_resolve_date(date))


def get_current(date: datetime.date | None = None) -> dict | None:
    """Return pending['current'] or None."""
    record = load_pending(date)
    if not record:
        return None
    return record.get("current")


def is_expired(date: datetime.date | None = None) -> bool:
    """True if created_at is older than 36 hours AND status != approved."""
    record = load_pending(date)
    if not record:
        return False
    if record.get("status") == "approved":
        return False
    created_at = record.get("created_at", "")
    try:
        created = datetime.datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(tz=datetime.timezone.utc) - created
        return age.total_seconds() > _EXPIRY_HOURS * 3600
    except Exception:
        return False


def find_active_pending_date() -> datetime.date | None:
    """Return the date of the active pending summary (today or yesterday), or None."""
    today = datetime.date.today()
    for d in (today, today - datetime.timedelta(days=1)):
        record = load_pending(d)
        if not record:
            continue
        if record.get("status") not in ("active", "awaiting_approval"):
            continue
        if is_expired(d):
            continue
        return d
    return None


def has_active_pending() -> bool:
    """True if today or yesterday has a non-approved, non-expired pending summary."""
    return find_active_pending_date() is not None


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end]).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data)}")
    return data


def _apply_edit_with_llm(current: dict, instruction: str, version: int) -> dict:
    """Apply a user edit instruction via Haiku. Returns updated classification."""
    cfg = get_config()
    model: str = cfg.get(
        "anthropic_cursor_presummary_model", "claude-haiku-4-5-20251001"
    )

    norm = _normalize_classification(current)
    done = norm["done"]
    unfinished = norm["unfinished"]
    unclassified = norm["unclassified"]

    user_msg = (
        f"Current summary (version {version}):\n"
        f"Done ({len(done)} items): {[t.get('task_name', '') for t in done]}\n"
        f"Unfinished ({len(unfinished)} items): "
        f"{[t.get('task_name', '') for t in unfinished]}\n"
        f"Unclassified ({len(unclassified)} items): "
        f"{[a.get('suggested_name', '') for a in unclassified]}\n\n"
        f"Full JSON:\n{json.dumps(norm, ensure_ascii=False, indent=2)}\n\n"
        f"User edit instruction: '{instruction}'\n\n"
        "Interpret any of these naturally:\n"
        "- 'Task X is done / completed / finished' → move X from unfinished to done\n"
        "- 'Task X is still in progress / not done yet' → keep/move to unfinished\n"
        "- 'Remove / delete / ignore item N or X' → remove from its section\n"
        "- 'Rename item N to Y' → change task_name or suggested_name\n"
        "- 'Add X as done' → append to done with confidence_score=1.0\n"
        "- 'Move X to unclassified' → move task to unclassified section\n"
        "- 'Change description of N to Y' → update reason or description\n"
        "Numbers refer to display order. Names can be partial matches.\n"
        "Return the complete updated JSON."
    )

    try:
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        msg = client.messages.create(
            model=model,
            max_tokens=2000,
            temperature=0,
            system=_EDIT_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        in_tok = msg.usage.input_tokens
        out_tok = msg.usage.output_tokens
        try:
            from utils.cost_logger import log_api_call
            log_api_call(model, in_tok, out_tok, "approval_edit")
        except Exception:
            pass

        updated = _extract_json(msg.content[0].text)
        result = _normalize_classification(updated)
        # Preserve metadata fields if LLM omitted them
        for key in ("day_theme", "time_breakdown", "unmatched_segments"):
            if key not in result and key in norm:
                result[key] = norm[key]
        log.info("Approval edit applied (v%d → LLM): %d in + %d out tokens", version, in_tok, out_tok)
        return result
    except Exception as exc:
        log.warning("approval_edit LLM failed (%s) — keeping current unchanged", exc)
        return copy.deepcopy(norm)


def apply_edit(edit_instruction: str, date: datetime.date | None = None) -> dict:
    """
    Save history snapshot, apply edit via LLM, increment version, save file.

    Returns the new current classification dict.
    """
    if date is None:
        date = find_active_pending_date() or _resolve_date(None)

    record = load_pending(date)
    if not record:
        raise ValueError(f"No pending summary for {date}")

    if record.get("status") != "awaiting_approval":
        raise ValueError(f"Pending summary for {date} is not awaiting approval")

    current = copy.deepcopy(record["current"])
    version = int(record.get("version", 1))

    record["history"].append({
        "version": version,
        "edit_instruction": edit_instruction,
        "snapshot": current,
    })

    new_current = _apply_edit_with_llm(current, edit_instruction, version)
    record["version"] = version + 1
    record["current"] = new_current
    _write_file(record, date)
    log.info("Pending summary updated for %s → v%d", date, record["version"])
    return new_current


def update_current(classification: dict, date: datetime.date | None = None) -> dict:
    """
    Directly replace current with a new classification (no LLM). Used by the
    web UI approve endpoint which sends the full updated state.
    Increments version and saves history snapshot.
    """
    if date is None:
        date = find_active_pending_date() or _resolve_date(None)
    record = load_pending(date)
    if not record:
        raise ValueError(f"No pending summary for {date}")
    current = copy.deepcopy(record["current"])
    version = int(record.get("version", 1))
    record["history"].append({
        "version": version,
        "edit_instruction": "web_ui_approve",
        "snapshot": current,
    })
    record["version"] = version + 1
    record["current"] = _normalize_classification(classification)
    _write_file(record, date)
    log.info("Pending summary updated (direct) for %s → v%d", date, record["version"])
    return record["current"]


def save_reviewed(date: datetime.date | None = None) -> dict:
    """Set status=reviewed, reviewed_at=now. Returns the full pending record."""
    if date is None:
        date = find_active_pending_date() or _resolve_date(None)

    record = load_pending(date)
    if not record:
        raise ValueError(f"No pending summary for {date}")

    record["status"] = "reviewed"
    record["reviewed_at"] = _now_iso()
    _write_file(record, date)
    log.info("Pending summary reviewed for %s (v%d)", date, record.get("version", 1))
    return record


save_approved = save_reviewed  # backward compat
