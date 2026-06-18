"""
Notion Tasks & Projects DB collector.

Reads from two Notion databases:
  Tasks DB    (config["notion_tasks_db_id"])
  Projects DB (config["notion_projects_db_id"])

Public API
----------
get_today_tasks(date=None)    → list[dict]   Status ∈ Today|In-Progress, Scheduled = date
get_classifiable_tasks(date=None) → list[dict]  same as get_today_tasks (for classifier)
get_this_week_tasks(date=None)→ list[dict]   Status ∈ This-Week|Today|In-Progress|Done, week
get_active_projects()         → list[dict]   Status = Active, sorted by priority
mark_task_done(page_id, actual_minutes=None)        → None
mark_task_deferred(page_id, to_date)                → None
create_task(task_name, ...)                         → str  (new page_id)

Task dict schema
----------------
{
  "page_id": str,
  "task_name": str,
  "project": str,          # project name, "" if no relation
  "status": str,
  "priority": str,         # "P1 High" | "P2 Medium" | "P3 Low"
  "estimated_minutes": int | None,
  "notes": str,
  "target_count": int,       # from "Target Count" property, default 1 if blank
  "recurrence": str,         # from "Recurrence" property, default "None"
  "completed_date": str | None,   # ISO date string, only in get_this_week_tasks
}

Project dict schema
-------------------
{
  "page_id": str,
  "name": str,
  "goal": str,
  "current_focus": str,
  "priority": str,
  "status": str,   # Projects DB Status select (e.g. "🟢 Active")
}

CLI
---
    python collectors/collect_tasks.py --today [--date YYYY-MM-DD] [--dry-run]
    python collectors/collect_tasks.py --week  [--date YYYY-MM-DD] [--dry-run]
    python collectors/collect_tasks.py --projects [--dry-run]
"""

from __future__ import annotations

import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger
from utils.notion_client import get_notion

log = get_logger("collect_tasks")

# ── in-process project name cache ────────────────────────────────────────────
_PROJECT_CACHE: dict[str, str] = {}


# ── Property extraction helpers ───────────────────────────────────────────────

def _rich_text_str(prop: dict) -> str:
    """Extract plain text from a Notion rich_text property."""
    return "".join(rt.get("plain_text", "") for rt in (prop.get("rich_text") or []))


def _title_str(prop: dict) -> str:
    """Extract plain text from a Notion title property."""
    return "".join(rt.get("plain_text", "") for rt in (prop.get("title") or []))


def _select_name(prop: dict) -> str:
    sel = prop.get("select") or {}
    return sel.get("name", "")


def _number_val(prop: dict) -> int | None:
    v = prop.get("number")
    return int(v) if v is not None else None


def _date_start(prop: dict) -> str | None:
    d = prop.get("date") or {}
    return d.get("start")


# ── Project name resolution (cached) ─────────────────────────────────────────

def _resolve_project_name(page_id: str) -> str:
    """Fetch the project page and return its Name property. Cached per process."""
    if page_id in _PROJECT_CACHE:
        return _PROJECT_CACHE[page_id]
    try:
        notion = get_notion()
        page = notion.pages.retrieve(page_id=page_id)
        name = _title_str(page["properties"].get("Name", {}))
        _PROJECT_CACHE[page_id] = name
        return name
    except Exception as exc:
        log.warning("Failed to resolve project %s: %s", page_id[:8], exc)
        _PROJECT_CACHE[page_id] = ""
        return ""


# ── Page → dict converters ────────────────────────────────────────────────────

def _page_to_task(page: dict, *, include_completed_date: bool = False) -> dict:
    """Convert a Notion Tasks DB page to our canonical task dict."""
    props = page.get("properties", {})

    # Project relation — take first relation entry
    relation = props.get("Project", {}).get("relation") or []
    project = _resolve_project_name(relation[0]["id"]) if relation else ""

    task: dict = {
        "page_id":           page["id"],
        "task_name":         _title_str(props.get("Task Name", {})),
        "project":           project,
        "status":            _select_name(props.get("Status", {})),
        "priority":          _select_name(props.get("Priority", {})),
        "estimated_minutes": _number_val(props.get("Estimated Duration", {})),
        "notes":             _rich_text_str(props.get("Notes", {})),
        "target_count":      _number_val(props.get("Target Count", {})) or 1,
        "recurrence":        _select_name(props.get("Recurrence", {})) or "None",
    }
    if include_completed_date:
        task["completed_date"] = _date_start(props.get("Completed Date", {}))
    return task


def _page_to_project(page: dict) -> dict:
    """Convert a Notion Projects DB page to our canonical project dict."""
    props = page.get("properties", {})
    return {
        "page_id":       page["id"],
        "name":          _title_str(props.get("Name", {})),
        "goal":          _rich_text_str(props.get("Goal", {})),
        "current_focus": _rich_text_str(props.get("Current Focus", {})),
        "priority":      _select_name(props.get("Priority", {})),
        "status":        _select_name(props.get("Status", {})),
    }


# ── Paginated query helper ────────────────────────────────────────────────────

def _query_all(database_id: str, filter_body: dict | None, sorts: list | None) -> list[dict]:
    """Run a paginated databases.query and return all result pages."""
    notion = get_notion()
    results: list[dict] = []
    cursor: str | None = None
    while True:
        kwargs: dict = {"database_id": database_id, "page_size": 100}
        if filter_body:
            kwargs["filter"] = filter_body
        if sorts:
            kwargs["sorts"] = sorts
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


# ── Date helpers ──────────────────────────────────────────────────────────────

def _week_bounds(date: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Return (Monday, Sunday) of the ISO week containing date."""
    dow = date.isoweekday()          # Monday=1, Sunday=7
    monday = date - datetime.timedelta(days=dow - 1)
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday


# ── Public read API ───────────────────────────────────────────────────────────

def get_today_tasks(date: datetime.date | None = None) -> list[dict]:
    """
    Query Tasks DB where:
      Status IN ["🎯 Today", "⚡ In Progress", "📅 This Week"]

    No Scheduled Date filter — tasks are eligible regardless of whether a date
    was filled in.  Returns tasks sorted by Priority ascending (P1 first).
    """
    if date is None:
        date = datetime.date.today()
    cfg = get_config()
    db_id: str = cfg["notion_tasks_db_id"]

    filter_body = {
        "or": [
            {"property": "Status", "select": {"equals": "🎯 Today"}},
            {"property": "Status", "select": {"equals": "⚡ In Progress"}},
            {"property": "Status", "select": {"equals": "📅 This Week"}},
        ]
    }
    sorts = [{"property": "Priority", "direction": "ascending"}]

    try:
        pages = _query_all(db_id, filter_body, sorts)
        tasks = [_page_to_task(p) for p in pages]
        log.info("get_today_tasks(%s): %d task(s)", date, len(tasks))
        return tasks
    except Exception as exc:
        log.warning("get_today_tasks failed: %s", exc)
        return []


def get_classifiable_tasks(date: datetime.date | None = None) -> list[dict]:
    """
    Return tasks eligible for end-of-day classification.

    Status ∈ Today | In-Progress | This Week.  No Scheduled Date required.
    Each dict includes target_count and recurrence for the classifier.
    """
    return get_today_tasks(date)


def get_this_week_tasks(date: datetime.date | None = None) -> list[dict]:
    """
    Query Tasks DB where:
      Status IN ["📅 This Week", "🎯 Today", "⚡ In Progress", "✅ Done"]
      AND Scheduled Date falls within the ISO week containing date

    Returns tasks with an additional "completed_date" field.
    """
    if date is None:
        date = datetime.date.today()
    cfg = get_config()
    db_id: str = cfg["notion_tasks_db_id"]

    monday, sunday = _week_bounds(date)
    filter_body = {
        "and": [
            {
                "or": [
                    {"property": "Status", "select": {"equals": "📅 This Week"}},
                    {"property": "Status", "select": {"equals": "🎯 Today"}},
                    {"property": "Status", "select": {"equals": "⚡ In Progress"}},
                    {"property": "Status", "select": {"equals": "✅ Done"}},
                ]
            },
            {
                "property": "Scheduled Date",
                "date": {"on_or_after": str(monday)},
            },
            {
                "property": "Scheduled Date",
                "date": {"on_or_before": str(sunday)},
            },
        ]
    }
    sorts = [{"property": "Priority", "direction": "ascending"}]

    try:
        pages = _query_all(db_id, filter_body, sorts)
        tasks = [_page_to_task(p, include_completed_date=True) for p in pages]
        log.info("get_this_week_tasks(%s–%s): %d task(s)", monday, sunday, len(tasks))
        return tasks
    except Exception as exc:
        log.warning("get_this_week_tasks failed: %s", exc)
        return []


def format_active_projects_prompt(active_projects: list[dict]) -> str:
    """
    Render active Notion projects for LLM prompts (goal + current focus per project).
    """
    if not active_projects:
        return "(no active projects in Projects DB)"
    blocks: list[str] = []
    for i, p in enumerate(active_projects, 1):
        pri = p.get("priority", "")
        name = p.get("name", "")
        goal = (p.get("goal") or "").strip()
        focus = (p.get("current_focus") or "").strip()
        status = (p.get("status") or "").strip()
        pid = p.get("page_id", "")
        header = f"[{i}] [{pri}] {name}"
        if status:
            header += f" (Status: {status})"
        if pid:
            header += f"  page_id={pid}"
        parts = [header]
        if goal:
            parts.append(f"Goal: {goal}")
        if focus:
            parts.append(f"Current focus:\n{focus}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def get_active_projects() -> list[dict]:
    """
    Query Projects DB where Status = "🟢 Active".
    Returns projects sorted by Priority ascending.
    """
    cfg = get_config()
    db_id: str = cfg["notion_projects_db_id"]

    filter_body = {"property": "Status", "select": {"equals": "🟢 Active"}}
    sorts = [{"property": "Priority", "direction": "ascending"}]

    try:
        pages = _query_all(db_id, filter_body, sorts)
        projects = [_page_to_project(p) for p in pages]
        log.info("get_active_projects: %d project(s)", len(projects))
        return projects
    except Exception as exc:
        log.warning("get_active_projects failed: %s", exc)
        return []


# ── Public write API ──────────────────────────────────────────────────────────

def mark_task_done(page_id: str, actual_minutes: int | None = None) -> None:
    """
    Update task page:
      Status         → "✅ Done"
      Completed Date → today (ISO date)
      Actual Duration→ actual_minutes (only if provided)
    """
    notion = get_notion()
    today = datetime.date.today()
    props: dict = {
        "Status":         {"select": {"name": "✅ Done"}},
        "Completed Date": {"date": {"start": str(today)}},
    }
    if actual_minutes is not None:
        props["Actual Duration"] = {"number": actual_minutes}
    try:
        notion.pages.update(page_id=page_id, properties=props)
        log.info("mark_task_done: %s  actual=%s min", page_id[:8], actual_minutes)
    except Exception as exc:
        log.error("mark_task_done failed for %s: %s", page_id[:8], exc)
        raise


def mark_task_deferred(page_id: str, to_date: datetime.date) -> None:
    """
    Update task page:
      Status         → "⏸ Deferred"
      Scheduled Date → to_date
    """
    notion = get_notion()
    props: dict = {
        "Status":         {"select": {"name": "⏸ Deferred"}},
        "Scheduled Date": {"date": {"start": str(to_date)}},
    }
    try:
        notion.pages.update(page_id=page_id, properties=props)
        log.info("mark_task_deferred: %s → %s", page_id[:8], to_date)
    except Exception as exc:
        log.error("mark_task_deferred failed for %s: %s", page_id[:8], exc)
        raise


def create_task(
    task_name: str,
    project_page_id: str | None = None,
    status: str = "📋 Backlog",
    priority: str = "P2 Medium",
    scheduled_date: datetime.date | None = None,
    estimated_minutes: int | None = None,
    notes: str = "",
) -> str:
    """
    Create a new task row in the Tasks DB.

    Returns the new page_id.
    Used by the agent when the user adds a task via Telegram reply.
    """
    notion = get_notion()
    cfg = get_config()
    db_id: str = cfg["notion_tasks_db_id"]

    props: dict = {
        "Task Name": {"title": [{"type": "text", "text": {"content": task_name}}]},
        "Status":    {"select": {"name": status}},
        "Priority":  {"select": {"name": priority}},
    }
    if project_page_id:
        props["Project"] = {"relation": [{"id": project_page_id}]}
    if scheduled_date is not None:
        props["Scheduled Date"] = {"date": {"start": str(scheduled_date)}}
    if estimated_minutes is not None:
        props["Estimated Duration"] = {"number": estimated_minutes}
    if notes:
        # Notion rich_text has a 2000-char limit per block
        props["Notes"] = {
            "rich_text": [{"type": "text", "text": {"content": notes[:2000]}}]
        }

    try:
        page = notion.pages.create(
            parent={"database_id": db_id},
            properties=props,
        )
        page_id: str = page["id"]
        log.info("create_task: %r  status=%s  priority=%s  →  %s",
                 task_name[:60], status, priority, page_id[:8])
        return page_id
    except Exception as exc:
        log.error("create_task failed for %r: %s", task_name[:60], exc)
        raise


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Notion Tasks & Projects DB collector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--today",    action="store_true", help="Show today's tasks")
    parser.add_argument("--week",     action="store_true", help="Show this week's tasks")
    parser.add_argument("--projects", action="store_true", help="Show active projects")
    parser.add_argument("--date",     default="today",
                        help="Date for --today / --week (YYYY-MM-DD or 'today')")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print with rich table (no effect on Notion)")
    parser.add_argument("--json",     action="store_true",
                        help="Output raw JSON instead of rich table")
    args = parser.parse_args()

    date = (
        datetime.date.today()
        if args.date == "today"
        else datetime.date.fromisoformat(args.date)
    )

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        use_rich = not args.json
    except ImportError:
        use_rich = False
        console = None

    def _print_tasks(tasks: list[dict], title: str) -> None:
        if args.json:
            print(json.dumps(tasks, indent=2, ensure_ascii=False, default=str))
            return
        if not use_rich:
            for t in tasks:
                print(f"  [{t['priority']}] {t['task_name']} — {t['project']}")
            return
        t = Table(title=title, show_header=True, header_style="bold")
        t.add_column("#",        style="dim",  width=3,  justify="right")
        t.add_column("Priority", width=10)
        t.add_column("Task",     min_width=25, overflow="fold")
        t.add_column("Project",  style="cyan", overflow="fold")
        t.add_column("Status",   width=16)
        t.add_column("Est.",     justify="right", style="dim")
        _PRI_COLOR = {"P1 High": "red", "P2 Medium": "yellow", "P3 Low": "green"}
        for i, task in enumerate(tasks, 1):
            pri = task.get("priority", "")
            color = _PRI_COLOR.get(pri, "")
            est = task.get("estimated_minutes")
            t.add_row(
                str(i),
                f"[{color}]{pri}[/]" if color else pri,
                task.get("task_name", ""),
                task.get("project", ""),
                task.get("status", ""),
                f"{est}m" if est else "—",
            )
        console.print(t)
        if not tasks:
            console.print(f"  [dim](no tasks found)[/]")

    def _print_projects(projects: list[dict], title: str) -> None:
        if args.json:
            print(json.dumps(projects, indent=2, ensure_ascii=False, default=str))
            return
        if not use_rich:
            for p in projects:
                print(f"  [{p['priority']}] {p['name']}: {p['goal']}")
            return
        t = Table(title=title, show_header=True, header_style="bold")
        t.add_column("Priority", width=10)
        t.add_column("Name",     min_width=20)
        t.add_column("Goal",     overflow="fold")
        t.add_column("Focus",    overflow="fold", style="dim")
        _PRI_COLOR = {"P1 High": "red", "P2 Medium": "yellow", "P3 Low": "green"}
        for p in projects:
            pri = p.get("priority", "")
            color = _PRI_COLOR.get(pri, "")
            t.add_row(
                f"[{color}]{pri}[/]" if color else pri,
                p.get("name", ""),
                p.get("goal", ""),
                p.get("current_focus", ""),
            )
        console.print(t)
        if not projects:
            console.print(f"  [dim](no active projects found)[/]")

    ran_any = False

    if args.today:
        ran_any = True
        tasks = get_today_tasks(date)
        _print_tasks(tasks, f"Today's Tasks — {date}")

    if args.week:
        ran_any = True
        monday, sunday = _week_bounds(date)
        tasks = get_this_week_tasks(date)
        _print_tasks(tasks, f"This Week's Tasks — {monday} → {sunday}")

    if args.projects:
        ran_any = True
        projects = get_active_projects()
        _print_projects(projects, "Active Projects")

    if not ran_any:
        parser.print_help()


if __name__ == "__main__":
    _cli()
