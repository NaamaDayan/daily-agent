"""
Plan store.

Persists daily plans as JSON files under config["plans_dir"].
Files are named YYYY-MM-DD.json.

Public API
----------
save_plan(plan, date=None, **metadata)   Write plan to disk (returns None).
load_plan(date=None) -> list[dict]       Load task list ([] if not found). Default: today.
update_plan(instruction, date=None)      Edit plan via Claude, save, return updated list.
format_plan_for_telegram(plan) -> str    Numbered list with 🔴/🟡/🟢 priority emojis.

Backward-compat helpers (used by run_daily + telegram_webhook)
--------------------------------------------------------------
load_plan_tasks(date)    alias for load_plan()
save_plan_tasks(tasks, date)  overwrite just the task list, preserving metadata
mark_task_done(task_id, date) toggle done flag
list_plans()             sorted [(date, path)] of all saved files
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

log = get_logger("plan_store")

# ── constants ─────────────────────────────────────────────────────────────────

_VALID_PRIORITIES = {"high", "medium", "low"}
_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}

_UPDATE_SYSTEM = (
    "You are a plan editor. Apply the user's edit to the JSON plan exactly as "
    "requested. Return the updated plan as a JSON array only — same schema, "
    "no explanation, no markdown fences."
)


# ── file helpers ──────────────────────────────────────────────────────────────

def _plans_dir() -> pathlib.Path:
    cfg = get_config()
    d = pathlib.Path(cfg.get("plans_dir", "~/.daily-agent/plans")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plan_path(date: datetime.date) -> pathlib.Path:
    return _plans_dir() / f"{date}.json"


def _load_payload(date: datetime.date) -> dict | None:
    """Return the full stored JSON payload, or None if the file does not exist."""
    path = _plan_path(date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to read plan file %s: %s", path, exc)
        return None


def _write_payload(payload: dict, date: datetime.date) -> None:
    path = _plan_path(date)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ── schema normalisation ──────────────────────────────────────────────────────

def _normalise_tasks(raw: list) -> list[dict]:
    """
    Normalise a raw task list from Claude or disk:
    - Ensure id, task, priority, context, done are present
    - Clamp invalid priority values to "medium"
    - Re-number ids 1..N if any are missing
    """
    if not isinstance(raw, list):
        raise ValueError(f"Expected a JSON array of tasks, got {type(raw).__name__}")

    tasks: list[dict] = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Task {i} is not an object: {item!r}")
        pri = str(item.get("priority", "medium")).lower().strip()
        if pri not in _VALID_PRIORITIES:
            log.debug("Clamping invalid priority %r → 'medium'", pri)
            pri = "medium"
        tasks.append({
            "id":       int(item["id"]) if "id" in item else i,
            "task":     str(item.get("task", "")),
            "priority": pri,
            "context":  str(item.get("context", "")),
            "done":     bool(item.get("done", False)),
        })
    return tasks


# ── Public API ────────────────────────────────────────────────────────────────

def save_plan(
    plan: list[dict],
    date: datetime.date | None = None,
    *,
    summary: str = "",
    highlights: list[str] | None = None,
    time_breakdown: list[dict] | None = None,
    blockers: list[str] | None = None,
    source_date: datetime.date | None = None,
) -> None:
    """
    Write *plan* to disk for *date* (default: today).

    Extra keyword args (summary, highlights, …) are stored as metadata and
    preserved by subsequent load_plan / update_plan calls.
    """
    if date is None:
        date = datetime.date.today()
    if source_date is None:
        source_date = datetime.date.today()

    normalised = _normalise_tasks(plan)

    payload = {
        "date":           str(date),
        "source_date":    str(source_date),
        "generated_at":   datetime.datetime.now(tz=datetime.timezone.utc).isoformat(
                              timespec="seconds"
                          ),
        "summary":        summary,
        "highlights":     highlights or [],
        "plan":           normalised,
        "time_breakdown": time_breakdown or [],
        "blockers":       blockers or [],
    }

    _write_payload(payload, date)
    log.info("Plan saved for %s (%d tasks) → %s", date, len(normalised), _plan_path(date))


def load_plan(date: datetime.date | None = None) -> list[dict]:
    """
    Return the task list for *date* (default: today).

    Returns an empty list if no plan file exists for that date.
    """
    if date is None:
        date = datetime.date.today()
    payload = _load_payload(date)
    if payload is None:
        log.debug("No plan file for %s", date)
        return []
    return payload.get("plan", [])


def update_plan(
    edit_instruction: str,
    date: datetime.date | None = None,
) -> list[dict]:
    """
    Apply *edit_instruction* (plain English) to the plan for *date* (default: today).

    Steps:
      a. Load current plan from disk.
      b. Call Claude (config["anthropic_model"], max_tokens=600, temperature=0).
      c. Parse + validate the returned JSON array.
      d. Save updated plan back to disk (preserving all metadata).
      e. Return the updated task list.

    Raises RuntimeError on API failure.
    Raises ValueError if Claude returns unparseable or invalid JSON.
    """
    if date is None:
        date = datetime.date.today()

    current_tasks = load_plan(date)
    if not current_tasks:
        raise RuntimeError(
            f"No plan found for {date}. "
            "Run the daily pipeline first, or save a plan manually."
        )

    cfg = get_config()
    model: str = cfg.get("anthropic_model", "claude-sonnet-4-6")
    max_tokens = 600

    plan_json = json.dumps(current_tasks, ensure_ascii=False)
    user_msg = (
        f"Current plan (JSON):\n{plan_json}\n\n"
        f"User edit: {edit_instruction}\n\n"
        "Return the updated plan as JSON only. Same schema. No explanation."
    )

    log.info("update_plan for %s: %r (model=%s)", date, edit_instruction[:60], model)

    try:
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            system=_UPDATE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = msg.content[0].text
        log.debug(
            "update_plan: %d input + %d output tokens",
            msg.usage.input_tokens, msg.usage.output_tokens,
        )
        try:
            from utils.cost_logger import log_api_call
            log_api_call(
                model, msg.usage.input_tokens, msg.usage.output_tokens,
                call_type="plan_update",
            )
        except Exception:
            pass
    except anthropic.APIError as exc:
        raise RuntimeError(f"Claude API error during plan update: {exc}") from exc

    # Strip optional code fences
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        raw = "\n".join(lines[1:end]).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Claude returned non-JSON for plan update: {exc}\n"
            f"Raw (first 300 chars): {raw[:300]}"
        ) from exc

    updated = _normalise_tasks(parsed)

    # Preserve metadata from existing payload
    payload = _load_payload(date) or {}
    payload["plan"] = updated
    payload["generated_at"] = datetime.datetime.now(
        tz=datetime.timezone.utc
    ).isoformat(timespec="seconds")
    _write_payload(payload, date)

    log.info("Plan updated for %s: %d tasks", date, len(updated))
    return updated


def format_plan_for_telegram(plan: list[dict]) -> str:
    """
    Format a plan list as a numbered Telegram-ready string.

    Priority emojis:  🔴 high  🟡 medium  🟢 low
    Done tasks are struck through with ~tildes~.

    Example:
        1. 🔴 Ship the summarizer — critical path item
        2. 🟡 Write tests — needed before merge
        3. 🟢 Clean up logging — nice to have
    """
    if not plan:
        return "(no tasks planned)"

    lines: list[str] = []
    for task in plan:
        pri = task.get("priority", "medium").lower()
        emoji = _PRIORITY_EMOJI.get(pri, "🟡")
        num = task.get("id", len(lines) + 1)
        text = task.get("task", "")
        ctx = task.get("context", "")
        done = task.get("done", False)

        body = f"~{text}~" if done else text
        line = f"{num}. {emoji} {body}"
        if ctx and not done:
            line += f" — {ctx}"
        lines.append(line)

    return "\n".join(lines)


# ── Backward-compat helpers ───────────────────────────────────────────────────

def load_plan_tasks(date: datetime.date | None = None) -> list[dict]:
    """Alias for load_plan(). Used by telegram_webhook.py."""
    return load_plan(date)


def save_plan_tasks(
    tasks: list[dict],
    date: datetime.date | None = None,
) -> None:
    """
    Overwrite just the task list for *date*, preserving all other metadata.
    Creates the file if it does not exist. Used by telegram_webhook.py.
    """
    if date is None:
        date = datetime.date.today()

    normalised = _normalise_tasks(tasks)
    payload = _load_payload(date) or {
        "date":           str(date),
        "source_date":    str(datetime.date.today()),
        "summary":        "",
        "highlights":     [],
        "time_breakdown": [],
        "blockers":       [],
    }
    payload["plan"] = normalised
    payload["generated_at"] = datetime.datetime.now(
        tz=datetime.timezone.utc
    ).isoformat(timespec="seconds")

    _write_payload(payload, date)
    log.info("Tasks updated for %s (%d tasks)", date, len(normalised))


def mark_task_done(
    task_id: int,
    date: datetime.date | None = None,
) -> bool:
    """
    Toggle the 'done' flag on the task with *task_id* in the plan for *date*.
    Returns True if the task was found and toggled, False otherwise.
    """
    if date is None:
        date = datetime.date.today()
    payload = _load_payload(date)
    if payload is None:
        return False
    for task in payload.get("plan", []):
        if task.get("id") == task_id:
            task["done"] = not task.get("done", False)
            _write_payload(payload, date)
            log.info("Task %d marked done=%s for %s", task_id, task["done"], date)
            return True
    return False


def list_plans() -> list[tuple[datetime.date, pathlib.Path]]:
    """Return a sorted list of (date, path) for all saved plan files."""
    result: list[tuple[datetime.date, pathlib.Path]] = []
    for p in _plans_dir().glob("*.json"):
        try:
            d = datetime.date.fromisoformat(p.stem)
            result.append((d, p))
        except ValueError:
            pass
    return sorted(result)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> datetime.date:
    if s == "today":
        return datetime.date.today()
    if s == "tomorrow":
        return datetime.date.today() + datetime.timedelta(days=1)
    return datetime.date.fromisoformat(s)


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Plan store — show, edit, or inspect daily plans",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python pipeline/plan_store.py --show
              python pipeline/plan_store.py --date 2026-05-28 --show
              python pipeline/plan_store.py --edit "move 2 to next week"
              python pipeline/plan_store.py --date 2026-05-30 --edit "add: review PRs as high priority"
              python pipeline/plan_store.py --list
        """),
    )
    parser.add_argument("--date", metavar="DATE",
                        help="YYYY-MM-DD, 'today', or 'tomorrow' (default: today)")
    parser.add_argument("--show", action="store_true",
                        help="Print the plan for --date")
    parser.add_argument("--edit", metavar="INSTRUCTION",
                        help="Apply a plain-English edit via Claude and save")
    parser.add_argument("--done", metavar="TASK_ID", type=int,
                        help="Toggle the done flag on task N")
    parser.add_argument("--list", action="store_true",
                        help="List all saved plan files")
    parser.add_argument("--telegram", action="store_true",
                        help="Print the Telegram-formatted plan for --date")
    args = parser.parse_args()

    date: datetime.date | None = _parse_date(args.date) if args.date else None
    effective_date = date or datetime.date.today()

    from rich.console import Console
    from rich.table import Table
    console = Console()

    # ── list ──────────────────────────────────────────────────────────────────
    if args.list:
        plans = list_plans()
        if not plans:
            console.print("[yellow]No plans saved yet.[/]")
            return
        t = Table(title="Saved plans", show_lines=False)
        t.add_column("Date", style="cyan")
        t.add_column("Tasks", justify="right")
        t.add_column("Generated at", style="dim")
        t.add_column("Path", style="dim", overflow="fold")
        for d, p in plans:
            payload = _load_payload(d) or {}
            t.add_row(
                str(d),
                str(len(payload.get("plan", []))),
                payload.get("generated_at", "")[:19],
                str(p),
            )
        console.print(t)
        return

    # ── edit ──────────────────────────────────────────────────────────────────
    if args.edit:
        console.print(
            f"[dim]Editing plan for {effective_date}: [italic]{args.edit}[/italic][/dim]"
        )
        try:
            updated = update_plan(args.edit, effective_date)
        except (RuntimeError, ValueError) as exc:
            console.print(f"[red]Error: {exc}[/]")
            return
        console.print(f"[green]✓ Plan updated ({len(updated)} tasks)[/]\n")
        _print_plan_table(console, updated, effective_date)
        return

    # ── done ──────────────────────────────────────────────────────────────────
    if args.done is not None:
        ok = mark_task_done(args.done, effective_date)
        if ok:
            console.print(f"[green]✓ Task {args.done} toggled.[/]")
            _print_plan_table(console, load_plan(effective_date), effective_date)
        else:
            console.print(
                f"[red]Task {args.done} not found in plan for {effective_date}.[/]"
            )
        return

    # ── telegram format ───────────────────────────────────────────────────────
    if args.telegram:
        plan = load_plan(effective_date)
        if not plan:
            console.print(f"[yellow]No plan for {effective_date}.[/]")
            return
        console.print(format_plan_for_telegram(plan))
        return

    # ── show (default) ────────────────────────────────────────────────────────
    if args.show or not any([args.list, args.edit, args.done, args.telegram]):
        plan = load_plan(effective_date)
        if not plan:
            console.print(f"[yellow]No plan for {effective_date}.[/]")
            return
        payload = _load_payload(effective_date) or {}
        gen = payload.get("generated_at", "")[:19]
        console.print(
            f"\n[bold]Plan for {effective_date}[/]"
            + (f"  [dim](generated {gen})[/dim]" if gen else "")
        )
        if payload.get("summary"):
            console.print(f"[italic dim]{payload['summary'][:200]}[/]\n")
        _print_plan_table(console, plan, effective_date)


def _print_plan_table(
    console,
    plan: list[dict],
    date: datetime.date,
) -> None:
    """Shared rich table renderer for a task list."""
    from rich.table import Table

    t = Table(show_header=True, header_style="bold", show_lines=False)
    t.add_column("#",        style="dim",    width=3,  justify="right")
    t.add_column("Priority", width=8)
    t.add_column("Task",     min_width=28)
    t.add_column("Context",  style="dim",    overflow="fold")
    t.add_column("✓",        width=2,        justify="center")

    _COLOR = {"high": "red", "medium": "yellow", "low": "green"}
    _LABEL = {"high": "HIGH", "medium": "MED", "low": "LOW"}

    for task in plan:
        pri = task.get("priority", "medium")
        color = _COLOR.get(pri, "")
        label = _LABEL.get(pri, pri.upper())
        text = task.get("task", "")
        if task.get("done"):
            text = f"[dim strike]{text}[/]"
        t.add_row(
            str(task.get("id", "")),
            f"[{color}]{_PRIORITY_EMOJI.get(pri,'?')} {label}[/]",
            text,
            task.get("context", ""),
            "✓" if task.get("done") else "",
        )

    console.print(t)


# textwrap needed for CLI epilog — import at module level
import textwrap  # noqa: E402  (after the function that uses it)

if __name__ == "__main__":
    _cli()
