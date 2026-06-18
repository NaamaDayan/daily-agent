"""
Token / API call logger CLI.

Wraps utils/cost_logger to show today's calls by type.

Usage:
    python utils/token_logger.py --today
    python utils/token_logger.py --today --type stage1_classify
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from utils.cost_logger import read_cost_log


def _print_today(call_type: str | None = None) -> None:
    today = datetime.date.today().isoformat()
    entries = [e for e in read_cost_log(days=1) if e.get("date") == today]
    if call_type:
        entries = [e for e in entries if e.get("call_type") == call_type]

    if not entries:
        label = f" (type={call_type})" if call_type else ""
        print(f"No API calls logged today{label}.")
        return

    print(f"API calls today ({today}) — {len(entries)} total\n")
    print(f"{'Type':<22} {'Model':<32} {'In':>7} {'Out':>7} {'Cost':>10}")
    print("-" * 82)
    total_cost = 0.0
    for e in entries:
        ct = e.get("call_type", "?")
        model = e.get("model", "?")
        in_t = e.get("input_tokens", 0)
        out_t = e.get("output_tokens", 0)
        cost = e.get("cost_usd", 0.0)
        total_cost += cost
        print(f"{ct:<22} {model:<32} {in_t:>7,} {out_t:>7,} ${cost:.4f}")

    print("-" * 82)
    print(f"{'TOTAL':<22} {'':<32} {'':<7} {'':<7} ${total_cost:.4f}")

    by_type: dict[str, int] = {}
    for e in entries:
        ct = e.get("call_type") or "unknown"
        by_type[ct] = by_type.get(ct, 0) + 1
    print("\nBy call_type:")
    for ct, count in sorted(by_type.items()):
        print(f"  {ct}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Token / API call logger")
    parser.add_argument(
        "--today", action="store_true",
        help="Show today's API calls from the cost log",
    )
    parser.add_argument(
        "--type", metavar="CALL_TYPE",
        help="Filter by call_type (e.g. stage1_classify, narrate, cursor_presummary)",
    )
    args = parser.parse_args()

    if args.today:
        _print_today(args.type)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
