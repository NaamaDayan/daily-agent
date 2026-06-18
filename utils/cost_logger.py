"""
API cost logger for the daily agent.

Every Claude API call writes one JSON line to ~/.daily-agent/cost-log.jsonl.
The log survives across pipeline runs and lets you track monthly spend.

JSONL schema (one object per line)
-----------------------------------
{
  "timestamp":      "2026-06-01T18:30:00+00:00",   # UTC ISO-8601
  "date":           "2026-06-01",                   # local calendar date
  "model":          "claude-sonnet-4-6",
  "call_type":      "synthesis",                    # see call sites below
  "input_tokens":   976,
  "output_tokens":  937,
  "cost_usd_input":  0.00293,
  "cost_usd_output": 0.01406,
  "cost_usd":        0.01699
}

call_type values used in this codebase
---------------------------------------
stage1_classify       task classifier (Stage 1C, Sonnet)
narrate               day_theme headline from classification (Sonnet)
activity_naming       unclassified activity cluster naming (Haiku)
approval_edit         pending summary edit during approval loop (Haiku)
synthesis           main EOD summary (Stage 2, Sonnet) [legacy]
cursor_presummary   per-session Cursor pre-summary (Stage 1A, haiku)
compression         per-source overflow compression (haiku)
plan_edit           Telegram plan-edit reply (haiku)
plan_update         plan_store.update_plan (Sonnet)
eod_parse           process_eod_task_replies (haiku)
micro_summary       micro_summarizer cluster summary (haiku)

Public API
----------
log_api_call(model, input_tokens, output_tokens, call_type="", extra=None)
estimate_cost(model, input_tokens, output_tokens) -> dict[str, float]
read_cost_log(days=30) -> list[dict]
print_cost_summary(days=30) -> None
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from utils.logger import get_logger

log = get_logger("cost_logger")

# ── Pricing table ─────────────────────────────────────────────────────────────
# USD per 1 000 000 tokens (input, output).
# Sources: https://www.anthropic.com/pricing  (update when Anthropic changes pricing)
# Override per-model via config.yaml: cost_per_m_input_sonnet / cost_per_m_output_sonnet

_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # model-name-prefix           ($/M input,  $/M output)
    "claude-sonnet-4-6":           (  3.00,   15.00),
    "claude-sonnet-4-5":           (  3.00,   15.00),
    "claude-3-7-sonnet":           (  3.00,   15.00),
    "claude-3-5-sonnet":           (  3.00,   15.00),
    "claude-3-sonnet":             (  3.00,   15.00),
    "claude-haiku-4-5-20251001":   (  0.80,    4.00),
    "claude-haiku-4-5":            (  0.80,    4.00),
    "claude-3-5-haiku":            (  0.80,    4.00),
    "claude-3-haiku":              (  0.25,    1.25),
    "claude-opus-4":               ( 15.00,   75.00),
    "claude-3-opus":               ( 15.00,   75.00),
}
_FALLBACK_PRICE = (3.00, 15.00)   # conservative (Sonnet-class) for unknown models


def _get_price(model: str) -> tuple[float, float]:
    """Look up ($/M input, $/M output) for a model. Uses longest prefix match."""
    if model in _PRICE_TABLE:
        return _PRICE_TABLE[model]
    # Sort longest key first so "claude-haiku-4-5-20251001" beats "claude-haiku-4-5"
    for key in sorted(_PRICE_TABLE, key=len, reverse=True):
        if model.startswith(key):
            return _PRICE_TABLE[key]
    return _FALLBACK_PRICE


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, float]:
    """
    Return ``{"input": cost_usd, "output": cost_usd, "total": cost_usd}``.

    Uses the built-in price table; does NOT read config (intentionally fast).
    """
    in_per_m, out_per_m = _get_price(model)
    cost_in  = in_per_m  * input_tokens  / 1_000_000
    cost_out = out_per_m * output_tokens / 1_000_000
    return {
        "input":  cost_in,
        "output": cost_out,
        "total":  cost_in + cost_out,
    }


# ── File path ─────────────────────────────────────────────────────────────────

def _cost_log_path() -> pathlib.Path:
    """Return the path to the JSONL cost log, creating parent dirs if needed."""
    try:
        from config_loader import get_config
        cfg  = get_config()
        base = pathlib.Path(
            cfg.get("pending_dir", "~/.daily-agent/pending")
        ).expanduser().parent          # ~/.daily-agent/pending → ~/.daily-agent
    except Exception:
        base = pathlib.Path("~/.daily-agent").expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base / "cost-log.jsonl"


# ── Write ─────────────────────────────────────────────────────────────────────

def log_api_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    call_type: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Log one API call to the rotating JSONL file and to the standard logger.

    This function is called after every ``client.messages.create()`` in the
    codebase. It never raises — a cost-logging failure must not abort the
    pipeline.

    Parameters
    ----------
    model         : Exact model name from the API response.
    input_tokens  : ``msg.usage.input_tokens``
    output_tokens : ``msg.usage.output_tokens``
    call_type     : Human-readable label for WHERE this call originated.
                    See module docstring for the list of values used here.
    extra         : Optional extra key/value pairs written to the JSONL line.
    """
    costs = estimate_cost(model, input_tokens, output_tokens)
    now   = datetime.datetime.now(tz=datetime.timezone.utc)

    # ── Log line to the standard logger (INFO) ────────────────────────────────
    log.info(
        "API cost  model=%-32s  type=%-20s  "
        "%d in + %d out tokens  $%.5f",
        model, call_type or "?",
        input_tokens, output_tokens,
        costs["total"],
    )

    # ── Append to JSONL ───────────────────────────────────────────────────────
    entry: dict[str, Any] = {
        "timestamp":       now.isoformat(timespec="seconds"),
        "date":            now.date().isoformat(),
        "model":           model,
        "call_type":       call_type,
        "input_tokens":    input_tokens,
        "output_tokens":   output_tokens,
        "cost_usd_input":  round(costs["input"],  8),
        "cost_usd_output": round(costs["output"], 8),
        "cost_usd":        round(costs["total"],  8),
    }
    if extra:
        entry.update(extra)

    try:
        path = _cost_log_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        # Cost logging must never break the pipeline
        log.debug("cost_logger write failed: %s", exc)


# ── Read ──────────────────────────────────────────────────────────────────────

def read_cost_log(days: int = 30) -> list[dict]:
    """
    Return all entries from the JSONL log that fall within the last *days*
    calendar days (inclusive of today).
    """
    path = _cost_log_path()
    if not path.exists():
        return []

    cutoff = (
        datetime.date.today() - datetime.timedelta(days=days - 1)
    ).isoformat()

    entries: list[dict] = []
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
                if entry.get("date", "") >= cutoff:
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return entries


# ── Print summary ─────────────────────────────────────────────────────────────

def print_cost_summary(days: int = 30) -> None:
    """Print a formatted cost summary for the last *days* calendar days."""
    entries = read_cost_log(days)
    try:
        _print_rich(entries, days)
    except ImportError:
        _print_plain(entries, days)


def _print_rich(entries: list[dict], days: int) -> None:
    from collections import defaultdict
    from rich.console import Console
    from rich.rule   import Rule
    from rich.table  import Table

    console = Console()
    console.print()
    console.print(Rule(
        f"[bold cyan]Daily Agent — Cost Summary  (last {days} days)[/]"
    ))

    if not entries:
        console.print()
        console.print("  [dim]No API calls logged yet.[/]")
        console.print(f"  [dim]Log file: {_cost_log_path()}[/]")
        console.print()
        return

    # ── Per-day breakdown ─────────────────────────────────────────────────────
    by_day: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "in_tok": 0, "out_tok": 0, "cost": 0.0}
    )
    for e in entries:
        d = e.get("date", "?")
        by_day[d]["calls"]   += 1
        by_day[d]["in_tok"]  += e.get("input_tokens", 0)
        by_day[d]["out_tok"] += e.get("output_tokens", 0)
        by_day[d]["cost"]    += e.get("cost_usd", 0.0)

    t = Table(
        show_header=True, header_style="bold",
        box=None, pad_edge=False, show_edge=False,
    )
    t.add_column("Date",        style="cyan",  width=12)
    t.add_column("Calls",       justify="right", width=6)
    t.add_column("Input tok",   justify="right", width=11)
    t.add_column("Output tok",  justify="right", width=11)
    t.add_column("Cost USD",    justify="right", width=10)

    for d in sorted(by_day):
        r = by_day[d]
        t.add_row(
            d,
            str(r["calls"]),
            f"{r['in_tok']:,}",
            f"{r['out_tok']:,}",
            f"${r['cost']:.4f}",
        )

    console.print()
    console.print(t)

    # ── Totals + projections ──────────────────────────────────────────────────
    total_calls = sum(r["calls"]   for r in by_day.values())
    total_in    = sum(r["in_tok"]  for r in by_day.values())
    total_out   = sum(r["out_tok"] for r in by_day.values())
    total_cost  = sum(r["cost"]    for r in by_day.values())
    active_days = len(by_day)
    daily_avg   = total_cost / active_days if active_days else 0.0

    console.print()
    console.print(
        f"  [bold]Total ({days}d):[/]  "
        f"{total_calls} calls  "
        f"[dim]{total_in:,} in + {total_out:,} out tokens[/]  "
        f"[bold green]${total_cost:.4f}[/]"
    )
    console.print(
        f"  [dim]Daily avg: ${daily_avg:.4f}  "
        f"→  projected 30-day: ${daily_avg * 30:.4f}[/]"
    )

    # ── By model ──────────────────────────────────────────────────────────────
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    for e in entries:
        m = e.get("model", "?")
        by_model[m]["calls"] += 1
        by_model[m]["cost"]  += e.get("cost_usd", 0.0)

    console.print()
    console.print("  [bold]By model:[/]")
    for m, v in sorted(by_model.items(), key=lambda x: -x[1]["cost"]):
        pct = 100.0 * v["cost"] / max(total_cost, 1e-9)
        bar = "▪" * max(1, round(pct / 5))   # 1 block per 5 %
        console.print(
            f"    [cyan]{m:<35}[/]  "
            f"{v['calls']:3} calls  "
            f"[green]${v['cost']:.4f}[/]  "
            f"[dim]{bar}[/]"
        )

    # ── By call type ──────────────────────────────────────────────────────────
    by_type: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    for e in entries:
        ct = e.get("call_type") or "unknown"
        by_type[ct]["calls"] += 1
        by_type[ct]["cost"]  += e.get("cost_usd", 0.0)

    console.print()
    console.print("  [bold]By call type:[/]")
    for ct, v in sorted(by_type.items(), key=lambda x: -x[1]["cost"]):
        pct = 100.0 * v["cost"] / max(total_cost, 1e-9)
        console.print(
            f"    [cyan]{ct:<22}[/]  "
            f"{v['calls']:3} call{'s' if v['calls'] != 1 else ' '}  "
            f"[green]${v['cost']:.4f}[/]  "
            f"[dim]({pct:.0f}%)[/]"
        )

    console.print()
    console.print(f"  [dim]Log: {_cost_log_path()}[/]")
    console.print()


def _print_plain(entries: list[dict], days: int) -> None:
    from collections import defaultdict

    W = 58
    print(f"\nDaily Agent — Cost Summary (last {days} days)")
    print("=" * W)

    if not entries:
        print("  No API calls logged yet.")
        print(f"  Log: {_cost_log_path()}")
        return

    by_day: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "in_tok": 0, "out_tok": 0, "cost": 0.0}
    )
    for e in entries:
        d = e.get("date", "?")
        by_day[d]["calls"]   += 1
        by_day[d]["in_tok"]  += e.get("input_tokens", 0)
        by_day[d]["out_tok"] += e.get("output_tokens", 0)
        by_day[d]["cost"]    += e.get("cost_usd", 0.0)

    print(f"{'Date':<12}  {'Calls':>5}  {'In tok':>9}  {'Out tok':>9}  {'Cost':>8}")
    print("-" * W)
    for d in sorted(by_day):
        r = by_day[d]
        print(
            f"{d:<12}  {r['calls']:5}  "
            f"{r['in_tok']:9,}  {r['out_tok']:9,}  "
            f"${r['cost']:.4f}"
        )

    total_cost  = sum(r["cost"]  for r in by_day.values())
    total_calls = sum(r["calls"] for r in by_day.values())
    active_days = len(by_day)
    print("-" * W)
    print(
        f"Total: {total_calls} calls  ${total_cost:.4f}  "
        f"({active_days} active days, "
        f"avg ${total_cost/max(active_days,1):.4f}/day)"
    )
    print(f"Log: {_cost_log_path()}")
    print()
