"""
Typing log collector.

Loads ~/.typing-log/YYYY-MM-DD.json, applies ignore/dedup/classification
filters, and returns a clean list of entries sorted by timestamp.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

# Allow running as a script from any cwd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("collect_typing")


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(raw: str) -> datetime.datetime:
    """Parse ISO-8601 timestamp; return epoch-min on failure."""
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _localize_naive_ts(dt: datetime.datetime) -> datetime.datetime:
    """Attach configured timezone to naive daemon timestamps (local wall clock)."""
    if dt.tzinfo is not None:
        return dt
    try:
        import pytz
        tz_name = get_config().get("timezone", "Asia/Jerusalem")
        return pytz.timezone(tz_name).localize(dt)
    except Exception:
        return dt.replace(tzinfo=datetime.timezone.utc)


def _coerce_timestamp_iso(raw: str) -> str:
    """Normalize to timezone-aware ISO string for timeline matching."""
    dt = _parse_ts(raw)
    if dt == datetime.datetime.min.replace(tzinfo=datetime.timezone.utc):
        return ""
    dt = _localize_naive_ts(dt)
    return dt.isoformat()


def _normalize_entry(raw: dict) -> dict | None:
    """
    Map daemon JSON fields to the canonical schema used downstream.

    Daemon writes: ts, bundle, window
    Pipeline expects: timestamp, bundle_id, window_title
    """
    ts_raw = raw.get("timestamp") or raw.get("ts")
    if not ts_raw:
        return None

    e = dict(raw)
    e["timestamp"] = _coerce_timestamp_iso(str(ts_raw))
    if not e["timestamp"]:
        return None
    e["bundle_id"] = (e.get("bundle_id") or e.get("bundle") or "").strip()
    e["window_title"] = e.get("window_title") or e.get("window") or ""
    e["app"] = e.get("app") or ""
    return e


def _build_deep_index(cfg: dict) -> dict[str, dict]:
    """Return {bundle_id: deep_app_config} from typing_deep_apps."""
    return {entry["bundle_id"]: entry for entry in cfg.get("typing_deep_apps", [])}


# ── classification ────────────────────────────────────────────────────────────

def _effective_mode(entry: dict, deep_index: dict, summary_set: set[str]) -> str:
    """
    Return the effective text mode for this entry:
      'full'      – keep text verbatim
      'summary'   – metadata only (text kept verbatim)
      'charcount' – metadata only (text kept verbatim)
    """
    bid = entry.get("bundle_id", "")

    if bid in deep_index:
        deep = deep_index[bid]
        raw_mode = deep.get("mode", "full")

        if raw_mode == "full":
            return "full"

        if raw_mode == "summary":
            return "summary"

        if raw_mode == "domain_filter":
            domains: list[str] = deep.get("domains", [])
            window_title: str = entry.get("window_title", "").lower()
            if any(d.lower() in window_title for d in domains):
                return "full"
            return "charcount"

    if bid in summary_set:
        return "summary"

    return "charcount"


# ── core pipeline ─────────────────────────────────────────────────────────────

def _process(entries: list[dict]) -> list[dict]:
    cfg = get_config()

    ignore_set: set[str] = set(cfg.get("typing_ignore_bundle_ids", []))
    min_len: int = cfg.get("typing_min_length", 15)
    dedup_window: int = cfg.get("typing_dedup_window_seconds", 300)
    deep_index = _build_deep_index(cfg)
    summary_set: set[str] = set(cfg.get("typing_summary_only_apps", []))

    # ── normalize daemon field names ──────────────────────────────────────────
    normalized: list[dict] = []
    skipped = 0
    for raw in entries:
        n = _normalize_entry(raw)
        if n is None:
            skipped += 1
            continue
        normalized.append(n)
    if skipped:
        log.debug("Skipped %d typing entries without timestamp", skipped)
    entries = normalized

    # ── sort by timestamp ascending ───────────────────────────────────────────
    entries = sorted(entries, key=lambda e: _parse_ts(e.get("timestamp", "")))

    # ── a) drop ignored bundle IDs ────────────────────────────────────────────
    entries = [e for e in entries if e.get("bundle_id", "") not in ignore_set]

    # ── b) drop short text ────────────────────────────────────────────────────
    entries = [e for e in entries if len(e.get("text", "").strip()) >= min_len]

    # ── c) deduplicate (bundle_id, text) within rolling window ────────────────
    last_kept: dict[tuple[str, str], datetime.datetime] = {}
    deduped: list[dict] = []
    for e in entries:
        key = (e.get("bundle_id", ""), e.get("text", "").strip())
        ts = _parse_ts(e.get("timestamp", ""))
        prev = last_kept.get(key)
        if prev is not None and (ts - prev).total_seconds() <= dedup_window:
            continue
        last_kept[key] = ts
        deduped.append(e)
    entries = deduped

    # ── d) classify entries (always keep verbatim text for timeline/classifier) ─
    result: list[dict] = []
    for e in entries:
        e = dict(e)  # shallow copy — don't mutate raw data
        text = e.get("text", "")
        mode = _effective_mode(e, deep_index, summary_set)

        # _mode records capture policy for legacy summarizer paths only.
        # Actual typed text is always preserved — required for task classification.
        e["_mode"] = mode
        e["_original_chars"] = len(text)
        result.append(e)

    return result


# ── public API ────────────────────────────────────────────────────────────────

def load_date(d: datetime.date) -> list[dict]:
    """Load, filter, and return typing entries for *d*."""
    cfg = get_config()
    path = pathlib.Path(cfg["typing_log_dir"]) / f"{d.isoformat()}.json"
    if not path.exists():
        log.debug("Typing log not found: %s", path)
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read typing log %s: %s", path, exc)
        return []
    log.debug("Loaded %d raw entries from %s", len(raw), path)
    processed = _process(raw)
    log.debug("After filtering: %d entries", len(processed))
    return processed


def load_today(d: datetime.date | None = None) -> list[dict]:
    """Load today's entries (or *d* if provided)."""
    return load_date(d if d is not None else datetime.date.today())


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Typing log collector — load, filter, and inspect entries."
    )
    parser.add_argument(
        "--date",
        default=str(datetime.date.today()),
        help="Date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a summary table instead of raw output",
    )
    parser.add_argument(
        "--show-text",
        action="store_true",
        help="Also print first 80 chars of each 'full' entry",
    )
    args = parser.parse_args()

    try:
        d = datetime.date.fromisoformat(args.date)
    except ValueError:
        print(f"Invalid date: {args.date!r}  (expected YYYY-MM-DD)", file=sys.stderr)
        sys.exit(1)

    entries = load_date(d)

    if not entries:
        print(f"No entries found for {d}.")
        return

    if args.dry_run:
        from collections import defaultdict

        from rich.console import Console
        from rich.table import Table

        groups: dict[str, dict] = defaultdict(
            lambda: {"entries": 0, "original_chars": 0, "modes": set()}
        )
        for e in entries:
            app = e.get("app", "unknown")
            groups[app]["entries"] += 1
            groups[app]["original_chars"] += e.get("_original_chars", 0)
            groups[app]["modes"].add(e.get("_mode", "charcount"))

        table = Table(
            title=f"Typing log — {d}  ({len(entries)} entries after filtering)",
            show_lines=False,
        )
        table.add_column("App", style="cyan", no_wrap=True)
        table.add_column("Entries", justify="right", style="bright_green")
        table.add_column("Original chars", justify="right")
        table.add_column("Mode", style="magenta")

        for app, data in sorted(groups.items(), key=lambda x: -x[1]["original_chars"]):
            table.add_row(
                app,
                str(data["entries"]),
                f"{data['original_chars']:,}",
                ", ".join(sorted(data["modes"])),
            )

        console = Console()
        console.print(table)

        if args.show_text:
            from rich.panel import Panel

            console.print()
            for e in entries:
                if e.get("_mode") == "full":
                    snippet = e.get("text", "")[:80].replace("\n", "↵")
                    console.print(
                        Panel(
                            snippet,
                            title=f"[cyan]{e.get('app')}[/]  {e.get('timestamp', '')[:19]}",
                            expand=False,
                        )
                    )
    else:
        print(json.dumps(entries, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
