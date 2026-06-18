"""
Self-learning store — records approved classifications for few-shot injection.

Storage: config["learning_dir"]/YYYY-MM-DD.json

Public API
----------
record_approved_day(approved_classification, date=None) -> None
get_few_shot_examples(n=None) -> list[dict]
format_few_shot_for_prompt(examples) -> str
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("learning_store")

_MAX_FEW_SHOT_TOKENS = 800


def _learning_dir() -> pathlib.Path:
    cfg = get_config()
    d = pathlib.Path(cfg.get("learning_dir", "~/.daily-agent/learning")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _learning_path(date: datetime.date) -> pathlib.Path:
    return _learning_dir() / f"{date.isoformat()}.json"


def _now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _had_footprint(task: dict, *, outcome: str) -> bool:
    score = float(task.get("confidence_score", 0.0))
    if outcome == "done":
        return score > 0 or bool(task.get("evidence"))
    return score >= float(get_config().get("classification_min_threshold", 0.40))


def _build_classifications(approved: dict) -> list[dict]:
    rows: list[dict] = []
    for t in approved.get("done") or []:
        evidence = [str(e)[:120] for e in (t.get("evidence") or [])[:3]]
        rows.append({
            "task_name": t.get("task_name", ""),
            "project": t.get("project", ""),
            "outcome": "done",
            "confidence_score": float(t.get("confidence_score", 1.0)),
            "evidence_patterns": evidence,
            "had_digital_footprint": _had_footprint(t, outcome="done"),
        })
    for t in approved.get("unfinished") or []:
        rows.append({
            "task_name": t.get("task_name", ""),
            "project": t.get("project", ""),
            "outcome": "unfinished",
            "confidence_score": float(t.get("confidence_score", 0.0)),
            "evidence_patterns": [],
            "had_digital_footprint": _had_footprint(t, outcome="unfinished"),
        })
    return rows


def record_approved_day(
    approved_classification: dict,
    date: datetime.date | None = None,
) -> None:
    """
    Persist an approved day's classification for future few-shot learning.

    Called after Telegram approval. Creates/overwrites YYYY-MM-DD.json.
    """
    cfg = get_config()
    if not cfg.get("learning_enabled", True):
        return

    date = date or datetime.date.today()

    version_count = 1
    approved_at = _now_iso()
    try:
        from pipeline.pending_summary import load_pending
        pending = load_pending(date)
        if pending:
            version_count = int(pending.get("version", 1))
            approved_at = pending.get("approved_at") or approved_at
    except Exception:
        pass

    unclassified = (
        approved_classification.get("unclassified")
        or approved_classification.get("unclassified_activities")
        or []
    )
    time_breakdown = approved_classification.get("time_breakdown") or []
    total_active_minutes = sum(int(item.get("minutes", 0)) for item in time_breakdown)

    record = {
        "date": date.isoformat(),
        "approved_at": approved_at,
        "version_count": version_count,
        "classifications": _build_classifications(approved_classification),
        "unclassified_count": len(unclassified),
        "total_active_minutes": total_active_minutes,
        "work_start_time": None,
        "work_end_time": None,
        "focus_blocks": None,
        "task_durations": None,
        "edit_corrections": None,
    }

    path = _learning_path(date)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        "Learning record saved: %s (%d classifications)",
        path.name, len(record["classifications"]),
    )


def get_few_shot_examples(n: int | None = None) -> list[dict]:
    """
    Read the N most recent learning records, newest first.

    Returns [] if learning_dir is empty or learning_enabled=False.
    """
    cfg = get_config()
    if not cfg.get("learning_enabled", True):
        return []

    if n is None:
        n = int(cfg.get("learning_few_shot_count", 5))

    d = _learning_dir()
    files = sorted(d.glob("????-??-??.json"), reverse=True)
    examples: list[dict] = []
    for path in files[:n]:
        try:
            examples.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            log.warning("Failed to read learning file %s: %s", path.name, exc)
    return examples


def _format_one_example(example: dict) -> str:
    date = example.get("date", "?")
    lines = [f"[{date}] Approved classifications:"]
    for c in example.get("classifications") or []:
        name = c.get("task_name", "")
        if c.get("outcome") == "done":
            evidence = "; ".join((c.get("evidence_patterns") or [])[:2])
            lines.append(f"✅ '{name}' — evidence: {evidence or 'none recorded'}")
        elif c.get("outcome") == "unfinished":
            footprint = (
                "partial evidence"
                if c.get("had_digital_footprint")
                else "no footprint"
            )
            lines.append(f"❌ '{name}' — {footprint}")
    lines.append("---")
    return "\n".join(lines)


def format_few_shot_for_prompt(examples: list[dict]) -> str:
    """
    Format learning examples for classifier prompt injection.

    Keeps output under ~800 tokens. Newest examples take priority.
    Returns "" if examples is empty.
    """
    if not examples:
        return ""

    max_chars = _MAX_FEW_SHOT_TOKENS * 4
    header = "=== PAST CLASSIFICATION EXAMPLES (use as reference) ===\n"
    body_parts: list[str] = []

    for ex in examples:
        part = _format_one_example(ex)
        candidate = header + "\n".join(body_parts + [part])
        if _estimate_tokens(candidate) > _MAX_FEW_SHOT_TOKENS and body_parts:
            break
        body_parts.append(part)

    if not body_parts:
        single = header + _format_one_example(examples[0])
        if len(single) > max_chars:
            single = single[:max_chars] + "\n[... truncated ...]"
        return single

    return header + "\n".join(body_parts)
