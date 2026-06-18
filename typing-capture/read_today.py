#!/usr/bin/env python3
"""
read_today.py — Read and filter typing-capture logs.

Usage:
  python read_today.py                        # today, human-readable
  python read_today.py --date 2026-05-28      # specific date
  python read_today.py --app "Chrome"         # filter by app name
  python read_today.py --min-chars 50         # only entries >= 50 chars
  python read_today.py --json                 # raw JSON
  python read_today.py --agent                # LLM-ready grouped markdown
"""

import sys
import json
import datetime
import argparse
from pathlib import Path
from collections import defaultdict

LOG_DIR = Path.home() / "typing-logs"


def load(date_str: str) -> list:
    path = LOG_DIR / f"{date_str}.json"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading {path}: {e}", file=sys.stderr)
        return []


def _filter(entries: list, app: str, min_chars: int) -> list:
    if app:
        low = app.lower()
        entries = [e for e in entries if low in (e.get("app") or "").lower()]
    if min_chars:
        entries = [e for e in entries if len(e.get("text") or "") >= min_chars]
    return entries


def print_human(entries: list) -> None:
    total = sum(len(e.get("text") or "") for e in entries)
    by_app: dict = defaultdict(int)
    for e in entries:
        by_app[e.get("app") or "?"] += len(e.get("text") or "")

    print(f"── {len(entries)} entries, {total:,} chars ──\n")
    for app, chars in sorted(by_app.items(), key=lambda x: -x[1]):
        bar = "█" * min(30, chars // 50)
        print(f"  {app:<28} {chars:>6,}  {bar}")
    print()

    for e in entries:
        ts      = e.get("ts",      "?")
        app     = e.get("app",     "?")
        window  = e.get("window",  "")
        trigger = e.get("trigger", "?")
        text    = e.get("text",    "")
        label   = f"{app} › {window}" if window else app
        print(f"[{ts}] ({trigger}) {label}")
        snippet = text[:120].replace("\n", " ↵ ")
        if len(text) > 120:
            snippet += "…"
        print(f"  {snippet}\n")


def print_agent(entries: list) -> None:
    """
    Groups by app, deduplicates, truncates to 500 chars each.
    Formatted as markdown sections for LLM ingestion.
    """
    groups: dict = defaultdict(list)
    for e in entries:
        app  = e.get("app") or "Unknown"
        text = (e.get("text") or "").strip()
        if text:
            groups[app].append(text)

    for app, texts in groups.items():
        # Deduplicate: keep first occurrence of each unique 120-char prefix
        seen: dict = {}
        for t in texts:
            key = t[:120]
            if key not in seen:
                seen[key] = t
        unique = list(seen.values())

        print(f"### {app}")
        for t in unique:
            truncated = t[:500] + ("…" if len(t) > 500 else "")
            print(truncated)
            print("---")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Read typing-capture logs")
    ap.add_argument(
        "--date",
        default=datetime.date.today().isoformat(),
        metavar="YYYY-MM-DD",
        help="Date to read (default: today)",
    )
    ap.add_argument("--app",       metavar="NAME",  help="Filter by app name (substring, case-insensitive)")
    ap.add_argument("--min-chars", metavar="N",     type=int, default=0, help="Minimum entry length")
    ap.add_argument("--json",      dest="json_out", action="store_true",  help="Raw JSON output")
    ap.add_argument("--agent",     action="store_true",  help="LLM-ready grouped markdown")
    args = ap.parse_args()

    entries = load(args.date)
    if not entries:
        print(f"No entries for {args.date}.")
        return

    entries = _filter(entries, args.app or "", args.min_chars)
    if not entries:
        print("No matching entries.")
        return

    if args.json_out:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
    elif args.agent:
        print_agent(entries)
    else:
        print_human(entries)


if __name__ == "__main__":
    main()
