"""
Daily planner — per-project LLM task generation.

Generates specific tasks for tomorrow for each active project
that has a matching entry in the current weekly plan.

Public API
----------
plan_tomorrow(projects, all_tasks, weekly_plan, today_result,
              calendar_events, general_context, detail) -> list[dict]
    Returns [{"project": str, "tasks": [str, ...]}, ...]
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import anthropic

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("daily_planner")

_DETAIL_INSTRUCTIONS = {
    "low": 'Return task names only. Example: "Add feature X"',
    "medium": 'Append a one-line definition of done after each task name, separated by " — ". Example: "Add feature X — write + pass tests"',
    "high": 'Append DOD, rationale, and time estimate after each task name. Example: "Add feature X — write + pass tests (why: critical path, ~90 min)"',
}

_SYSTEM_TEMPLATE = """\
You are a precise daily task planner. Generate 1-3 specific, actionable tasks for tomorrow \
for the given project. Consider the project's weekly goals, existing tasks, and today's progress.

Return JSON only — no markdown fences, no explanation:
{{"tasks": ["task 1", "task 2"]}}

Task format: {detail_instruction}"""


def _call_project_daily_llm(
    *,
    project: dict,
    project_tasks: list[dict],
    weekly_goals: list[dict],
    today_result: dict,
    calendar_events: list[dict],
    general_context: str,
    detail: str,
) -> list[str]:
    """
    One LLM call for a single project. Returns a list of task strings.
    Raises on API error or JSON parse failure (caller handles per-project).
    """
    cfg = get_config()
    model: str = cfg.get("daily_planner_model", "claude-sonnet-4-6")
    max_tokens: int = int(cfg.get("daily_planner_max_tokens", 1000))

    detail_instruction = _DETAIL_INSTRUCTIONS.get(detail, _DETAIL_INSTRUCTIONS["medium"])
    system = _SYSTEM_TEMPLATE.format(detail_instruction=detail_instruction)

    goals_text = "\n".join(
        f"- [{g.get('priority', '')}] {g['goal']}: {g.get('rationale', '')}"
        for g in weekly_goals
    ) or "(none)"

    tasks_text = "\n".join(
        f"- [{t.get('priority', '')}] [{t.get('status', '')}] {t['task_name']}"
        for t in project_tasks[:30]
    ) or "(none)"

    done_text = "\n".join(
        f"- {t.get('task_name', t) if isinstance(t, dict) else t}"
        for t in (today_result.get("done") or [])[:10]
    ) or "(none)"

    unfinished_text = "\n".join(
        f"- {t.get('task_name', t) if isinstance(t, dict) else t}"
        for t in (today_result.get("unfinished") or [])[:5]
    ) or "(none)"

    calendar_text = "\n".join(
        f"- {e.get('start_time', '')}–{e.get('end_time', '')} {e.get('title', '')}"
        for e in calendar_events
    ) or "(no events)"

    user_msg = (
        f"Project: {project['name']}\n"
        f"Goal: {project.get('goal', '')}\n"
        f"Current focus: {project.get('current_focus', '')}\n\n"
        f"Project context:\n{project.get('full_text', '')[:1500]}\n\n"
        f"Unprocessed ideas:\n{project.get('unprocessed_tasks', '') or '(none)'}\n\n"
        f"Weekly goals for this project:\n{goals_text}\n\n"
        f"All open tasks:\n{tasks_text}\n\n"
        f"Today — theme: {today_result.get('day_theme', '')}\n"
        f"Done today:\n{done_text}\n"
        f"Unfinished:\n{unfinished_text}\n\n"
        f"Tomorrow's calendar:\n{calendar_text}\n\n"
        f"General context:\n{general_context[:400]}\n\n"
        "Generate 1-3 tasks for tomorrow for this project only."
    )

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = msg.content[0].text.strip()
    try:
        from utils.cost_logger import log_api_call
        log_api_call(
            model, msg.usage.input_tokens, msg.usage.output_tokens,
            call_type="daily_plan",
        )
    except Exception:
        pass

    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(raw)
    return parsed.get("tasks", [])


def plan_tomorrow(
    projects: list[dict],
    all_tasks: list[dict],
    weekly_plan: dict,
    today_result: dict,
    calendar_events: list[dict],
    general_context: str,
    detail: str = "medium",
) -> list[dict]:
    """
    Generate daily tasks for tomorrow across all projects.

    Matches projects to weekly plan entries by project name (case-insensitive).
    Calls LLM once per matched project; skips unmatched projects.
    Per-project LLM failures are isolated — other projects continue.

    Returns [{"project": str, "tasks": [str, ...]}, ...] — only projects
    with at least one task are included.
    """
    weekly_by_name: dict[str, list[dict]] = {
        p["project_name"].strip().lower(): p.get("weekly_goals", [])
        for p in weekly_plan.get("projects", [])
    }

    result: list[dict] = []
    for project in projects:
        project_name = project["name"]
        key = project_name.strip().lower()
        if key not in weekly_by_name:
            log.debug("No weekly goals for project %r — skipping", project_name)
            continue

        weekly_goals = weekly_by_name[key]
        project_tasks = [
            t for t in all_tasks
            if t["project"].strip().lower() == key
        ]

        try:
            tasks = _call_project_daily_llm(
                project=project,
                project_tasks=project_tasks,
                weekly_goals=weekly_goals,
                today_result=today_result,
                calendar_events=calendar_events,
                general_context=general_context,
                detail=detail,
            )
            if tasks:
                result.append({"project": project_name, "tasks": tasks})
            log.info("daily_planner: %r → %d task(s)", project_name, len(tasks))
        except Exception as exc:
            log.warning("daily_planner: LLM failed for %r: %s", project_name, exc)

    return result
