"""
Micro-summarizer — incremental 30-minute activity summaries.

Runs every 30 minutes during the day (separate cron: */30 9-20 * * *).
Generates one-sentence summaries per app cluster for the last 30-minute window
and appends them to ~/.daily-agent/micro/YYYY-MM-DD.jsonl.

At end-of-day, the main summarizer reads these micro-summaries and substitutes
them for raw typing entries, cutting input token cost from ~5 000 to ~2 000.

JSONL schema (one object per line):
  {
    "timestamp":    "<ISO when this entry was written>",
    "window_start": "<ISO start of 30-min window>",
    "window_end":   "<ISO end of 30-min window>",
    "app":          "Cursor",
    "minutes":      22.5,
    "summary":      "Implemented the timeline builder merge logic for the daily agent.",
    "model":        "claude-haiku-4-5-20251001",
    "tokens":       68
  }

Public API
----------
run_micro_summary(window_minutes=30, date=None, now=None) -> list[dict]
    Summarize the most recent window and append to the JSONL. Returns entries written.

get_micro_summaries(date=None) -> list[dict]
    Load all micro-summaries for *date* (default: today). Returns [] if no file.

CLI
---
    python pipeline/micro_summarizer.py --run
        Summarize the last 30 minutes and append to today's JSONL.

    python pipeline/micro_summarizer.py --show [--date YYYY-MM-DD]
        Print today's (or a specific date's) micro-summaries.

    python pipeline/micro_summarizer.py --run --dry-run
        Print what would be written without saving.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import anthropic

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("micro_summarizer")

# ── Bundle-id → display name (subset used for micro clustering) ───────────────

_BUNDLE_TO_APP: dict[str, str] = {
    "com.google.Chrome":               "Chrome",
    "com.apple.Safari":                "Safari",
    "org.mozilla.firefox":             "Firefox",
    "company.thebrowser.Browser":      "Arc",
    "com.todesktop.230313mzl4w4u92":   "Cursor",
    "com.microsoft.VSCode":            "VS Code",
    "io.claude.app":                   "Claude",
    "com.anthropic.claudefordesktop":  "Claude",
    "com.notion.mac":                  "Notion",
    "com.apple.Notes":                 "Notes",
    "com.tinyspeck.slackmacgap":       "Slack",
    "com.apple.mail":                  "Mail",
    "zoom.us":                         "Zoom",
    "com.googlecode.iterm2":           "Terminal",
    "com.apple.Terminal":              "Terminal",
}

_MIN_CLUSTER_MINUTES = 2.0   # skip clusters with < 2 min activity AND no typing
_MIN_CLUSTER_SECS    = _MIN_CLUSTER_MINUTES * 60


# ── Path helpers ──────────────────────────────────────────────────────────────

def _micro_dir() -> pathlib.Path:
    cfg = get_config()
    d = pathlib.Path(cfg.get("micro_dir", "~/.daily-agent/micro")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _micro_path(date: datetime.date) -> pathlib.Path:
    return _micro_dir() / f"{date}.jsonl"


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _to_utc(ts_str: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _ts_in_window(
    ts_str: str,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> bool:
    try:
        ts = _to_utc(ts_str)
        return window_start <= ts <= window_end
    except Exception:
        return False


def _event_overlaps_window(
    event: dict,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> bool:
    try:
        ev_s = _to_utc(event["start_iso"])
        ev_e = _to_utc(event["end_iso"])
        return ev_s < window_end and ev_e > window_start
    except Exception:
        return False


def _overlap_seconds(
    event: dict,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> float:
    """Return the number of seconds the event overlaps with the window."""
    try:
        ev_s = max(_to_utc(event["start_iso"]), window_start)
        ev_e = min(_to_utc(event["end_iso"]),   window_end)
        return max(0.0, (ev_e - ev_s).total_seconds())
    except Exception:
        return 0.0


# ── App clustering ────────────────────────────────────────────────────────────

def _bundle_to_display(bundle_id: str, app_fallback: str) -> str:
    """Map a bundle_id to a short human-readable app name."""
    if bundle_id in _BUNDLE_TO_APP:
        return _BUNDLE_TO_APP[bundle_id]
    # Fallback: last segment of the bundle or the app field from the event
    parts = bundle_id.split(".")
    last = parts[-1] if parts else ""
    return app_fallback or last or bundle_id


def _build_clusters(
    typing_entries: list[dict],
    aw_events: list[dict],
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> dict[str, dict]:
    """
    Group activity by app for the window.

    Returns:
        {app_name: {"typing": [str, ...], "seconds": float}}
    """
    clusters: dict[str, dict] = {}

    def _get(app: str) -> dict:
        if app not in clusters:
            clusters[app] = {"typing": [], "seconds": 0.0}
        return clusters[app]

    # ActivityWatch events → seconds per app
    for ev in aw_events:
        if not _event_overlaps_window(ev, window_start, window_end):
            continue
        app = ev.get("app", "Unknown")
        _get(app)["seconds"] += _overlap_seconds(ev, window_start, window_end)

    # Typing entries → text per bundle/app
    for entry in typing_entries:
        ts = entry.get("timestamp", "")
        if not ts or not _ts_in_window(ts, window_start, window_end):
            continue
        text = (entry.get("text") or "").strip()
        if not text or text.startswith("["):
            continue
        bundle = entry.get("bundle_id", "")
        app = _bundle_to_display(bundle, entry.get("app", ""))
        _get(app)["typing"].append(text)

    return clusters


# ── Claude call ───────────────────────────────────────────────────────────────

def _summarize_cluster(
    app: str,
    cluster: dict,
    model: str,
) -> tuple[str, int]:
    """
    Call the haiku model to produce a ONE-sentence summary.

    Returns (summary_text, total_tokens).
    """
    cfg = get_config()
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])

    mins = cluster["seconds"] / 60
    user_parts = [f"App: {app}", f"Duration: {mins:.0f} min"]
    if cluster["typing"]:
        user_parts.append("What was typed (samples):")
        for t in cluster["typing"][:5]:
            user_parts.append(f"  • {t[:150]}")
    user_parts.append(
        "\nSummarize in ONE sentence what was being done. "
        "Be specific about the task or topic — not just the app name."
    )

    msg = client.messages.create(
        model=model,
        max_tokens=150,
        temperature=0,
        system=(
            "You summarize short computer activity windows in exactly one specific "
            "sentence. Name the actual task or topic being worked on — not the app."
        ),
        messages=[{"role": "user", "content": "\n".join(user_parts)}],
    )
    text = msg.content[0].text.strip()
    # Ensure it's a single sentence
    if "\n" in text:
        text = text.splitlines()[0].strip()

    total_tokens = msg.usage.input_tokens + msg.usage.output_tokens
    try:
        from utils.cost_logger import log_api_call
        log_api_call(
            model, msg.usage.input_tokens, msg.usage.output_tokens,
            call_type="micro_summary",
        )
    except Exception:
        pass

    return text, total_tokens


# ── Public API ────────────────────────────────────────────────────────────────

def run_micro_summary(
    *,
    window_minutes: int = 30,
    date: datetime.date | None = None,
    now: datetime.datetime | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """
    Summarize activity in the most recent *window_minutes*-minute window.

    Appends one entry per non-trivial app cluster to
    ~/.daily-agent/micro/YYYY-MM-DD.jsonl.

    Parameters
    ----------
    window_minutes : Width of the look-back window (default 30).
    date           : Date being summarised (default: today).
    now            : Override "now" for testing (default: UTC wall clock).
    dry_run        : If True, compute summaries but do NOT write to disk.

    Returns the list of summary dicts written (or that would be written).
    """
    cfg = get_config()
    haiku_model: str = cfg.get(
        "anthropic_cursor_presummary_model", "claude-haiku-4-5-20251001"
    )

    if date is None:
        date = datetime.date.today()
    if now is None:
        now = datetime.datetime.now(tz=datetime.timezone.utc)

    window_end   = now
    window_start = now - datetime.timedelta(minutes=window_minutes)

    log.info(
        "Micro-summarizer: window %s → %s",
        window_start.isoformat(timespec="minutes"),
        window_end.isoformat(timespec="minutes"),
    )

    # ── Load typing entries ───────────────────────────────────────────────────
    try:
        from collectors.collect_typing import load_date
        all_typing = load_date(date)
    except Exception as exc:
        log.warning("Micro: typing load failed: %s", exc)
        all_typing = []

    # ── Load AW events ────────────────────────────────────────────────────────
    try:
        from collectors.collect_activitywatch import get_events_date
        all_events = get_events_date(date)
    except Exception as exc:
        log.warning("Micro: ActivityWatch load failed: %s", exc)
        all_events = []

    # ── Cluster activity by app ───────────────────────────────────────────────
    clusters = _build_clusters(all_typing, all_events, window_start, window_end)

    if not clusters:
        log.info("Micro: no activity in window — nothing to summarise")
        return []

    log.info("Micro: %d app cluster(s) to summarise", len(clusters))

    # ── Summarise each cluster ────────────────────────────────────────────────
    summaries: list[dict] = []
    for app, cluster in clusters.items():
        seconds = cluster["seconds"]
        has_typing = bool(cluster["typing"])

        if seconds < _MIN_CLUSTER_SECS and not has_typing:
            log.debug("Micro: skipping %s (%.0fs, no typing)", app, seconds)
            continue

        try:
            summary_text, tokens = _summarize_cluster(app, cluster, haiku_model)
        except Exception as exc:
            log.warning("Micro: summary failed for %s: %s", app, exc)
            continue

        entry: dict = {
            "timestamp":    now.isoformat(timespec="seconds"),
            "window_start": window_start.isoformat(timespec="seconds"),
            "window_end":   window_end.isoformat(timespec="seconds"),
            "app":          app,
            "minutes":      round(seconds / 60, 1),
            "summary":      summary_text,
            "model":        haiku_model,
            "tokens":       tokens,
        }
        summaries.append(entry)
        log.info("Micro [%s]: %s  (tokens=%d)", app, summary_text[:80], tokens)

    # ── Write to JSONL ────────────────────────────────────────────────────────
    if summaries and not dry_run:
        path = _micro_path(date)
        with path.open("a", encoding="utf-8") as f:
            for entry in summaries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.info("Micro: wrote %d entries to %s", len(summaries), path)
    elif dry_run:
        log.info("Micro: dry-run — not writing %d entries", len(summaries))

    return summaries


def get_micro_summaries(date: datetime.date | None = None) -> list[dict]:
    """
    Load all micro-summaries for *date* from the JSONL file.

    Returns an empty list if no file exists or on any read error.
    """
    if date is None:
        date = datetime.date.today()

    path = _micro_path(date)
    if not path.exists():
        return []

    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    except Exception as exc:
        log.warning("Failed to load micro-summaries for %s: %s", date, exc)
        return []

    log.info("Loaded %d micro-summaries for %s", len(entries), date)
    return entries


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Micro-summarizer — 30-minute incremental activity summaries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--run", action="store_true",
        help="Summarize the last 30 minutes and append to today's JSONL",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print micro-summaries for --date (default: today)",
    )
    parser.add_argument(
        "--date", default="today",
        help="Date (YYYY-MM-DD or 'today') for --show",
    )
    parser.add_argument(
        "--window", type=int, default=30, metavar="MINUTES",
        help="Look-back window in minutes for --run (default: 30)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="With --run: compute and print but do NOT write to disk",
    )
    args = parser.parse_args()

    date = (
        datetime.date.today()
        if args.date == "today"
        else datetime.date.fromisoformat(args.date)
    )

    if args.run:
        try:
            from rich.console import Console
            from rich.table import Table
            console = Console()
        except ImportError:
            console = None

        print(f"Running micro-summarizer (window={args.window}min, dry_run={args.dry_run})…")
        summaries = run_micro_summary(
            window_minutes=args.window,
            date=date,
            dry_run=args.dry_run,
        )

        if not summaries:
            print("(no activity to summarise)")
            return

        if console:
            t = Table(show_header=True, header_style="bold")
            t.add_column("App",     style="cyan")
            t.add_column("Min",     justify="right", style="dim")
            t.add_column("Summary", overflow="fold")
            t.add_column("Tokens",  justify="right", style="dim")
            for s in summaries:
                t.add_row(
                    s["app"],
                    f"{s['minutes']:.0f}",
                    s["summary"],
                    str(s["tokens"]),
                )
            console.print(t)
            if args.dry_run:
                console.print("[dim](dry-run — nothing written)[/]")
        else:
            for s in summaries:
                print(f"[{s['app']}] {s['summary']}")

    elif args.show:
        entries = get_micro_summaries(date)
        if not entries:
            print(f"No micro-summaries for {date}")
            return

        try:
            from rich.console import Console
            from rich.table import Table
            console = Console()
            t = Table(title=f"Micro-summaries for {date}", show_header=True)
            t.add_column("Window",  style="cyan", no_wrap=True)
            t.add_column("App")
            t.add_column("Min",     justify="right", style="dim")
            t.add_column("Summary", overflow="fold")
            for e in entries:
                ws = e.get("window_start", "")
                we = e.get("window_end", "")
                window_label = f"{ws[11:16]}–{we[11:16]}" if len(ws) >= 16 else ws[:16]
                t.add_row(
                    window_label,
                    e.get("app", "?"),
                    f"{e.get('minutes', 0):.0f}",
                    e.get("summary", ""),
                )
            console.print(t)
        except ImportError:
            for e in entries:
                print(json.dumps(e, ensure_ascii=False))

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
