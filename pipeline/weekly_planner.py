"""
Weekly planner — LLM-based weekly summary and per-project goal generation.

Public API
----------
load_week_day_summaries(week_start) -> list[dict]
    Read approved/pending daily summaries for Mon–Sat of the given week.

run_weekly_planning(week_start, general_context, week_tasks, all_tasks, project_pages) -> dict
    Runs full planning: one week-summary call + one per-project planning call.
    Returns a dict matching the weekly_store schema.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import anthropic

from config_loader import get_config
from pipeline.pending_summary import load_pending
from utils.logger import get_logger

log = get_logger("weekly_planner")

# ── Prompts ──────────────────────────────────────────────────────────────────

_WEEK_SUMMARY_SYSTEM = (
    "You are summarizing a developer's work week. "
    "Given daily themes and completed tasks, write a 2-3 sentence narrative. "
    "Return JSON only — no markdown fences.\n"
    'Schema: {"week_summary": str, "highlights": [str]}'
)

_PROJECT_PLAN_SYSTEM = (
    "You are a project manager planning next week's work for a developer. "
    "Given project context, backlog tasks, and weekly accomplishments, "
    "generate 3-5 broad directional weekly goals. "
    "These are NOT specific step-by-step tasks — they are directions that guide daily planning. "
    "Return JSON only — no markdown fences.\n"
    '{"weekly_goals": [{"goal": str, "rationale": str, "priority": "high"|"medium"|"low"}]}'
)


# ── Day summary loading ───────────────────────────────────────────────────────

def load_week_day_summaries(week_start: datetime.date) -> list[dict]:
    """
    Read pending/approved daily summaries for Mon–Sat of the week starting *week_start*.

    Returns list of dicts (one per day that has data):
        [{"date": str, "day_theme": str, "done_count": int, "unfinished_count": int}]
    Skips days with no pending file.
    """
    summaries: list[dict] = []
    for offset in range(6):  # Mon=0 through Sat=5
        day = week_start + datetime.timedelta(days=offset)
        try:
            record = load_pending(day)
        except Exception as exc:
            log.warning("Could not load pending for %s: %s", day, exc)
            continue
        if record is None:
            continue
        current = record.get("current") or {}
        summaries.append({
            "date":             str(day),
            "day_theme":        current.get("day_theme", ""),
            "done_count":       len(current.get("done") or []),
            "unfinished_count": len(current.get("unfinished") or []),
        })
    return summaries


# ── LLM calls (split out for testability) ────────────────────────────────────

def _call_week_summary_llm(
    day_summaries: list[dict],
    week_tasks: list[dict],
    general_context: str,
    week_label: str,
) -> dict:
    """Single LLM call to summarize the week. Returns {"week_summary": str, "highlights": [str]}."""
    cfg = get_config()
    model = cfg.get("weekly_summary_model", "claude-haiku-4-5-20251001")
    max_tokens = 600

    days_text = "\n".join(
        f"- {s['date']}: {s['day_theme']} ({s['done_count']} done, {s['unfinished_count']} unfinished)"
        for s in day_summaries
    ) or "(no daily summaries available)"

    done_tasks = [t for t in week_tasks if "Done" in t.get("status", "") or "✅" in t.get("status", "")]
    tasks_text = "\n".join(
        f"- [{t.get('priority','')}] {t['task_name']} — {t.get('project','')}"
        for t in done_tasks[:20]
    ) or "(no completed tasks recorded)"

    user_msg = (
        f"Week: {week_label}\n\n"
        f"Daily summaries:\n{days_text}\n\n"
        f"Completed tasks this week:\n{tasks_text}\n\n"
        f"General context:\n{general_context[:400]}\n\n"
        "Summarize this week."
    )

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=_WEEK_SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = msg.content[0].text.strip()
    log.debug("Week summary: %d in / %d out tokens", msg.usage.input_tokens, msg.usage.output_tokens)

    try:
        from utils.cost_logger import log_api_call
        log_api_call(model, msg.usage.input_tokens, msg.usage.output_tokens, call_type="weekly_summary")
    except Exception:
        pass

    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def _call_project_plan_llm(
    project: dict,
    project_tasks: list[dict],
    week_summary: str,
    general_context: str,
    next_week_label: str,
) -> dict:
    """One LLM call per project. Returns {"weekly_goals": [...]}."""
    cfg = get_config()
    model = cfg.get("weekly_planner_model", "claude-sonnet-4-6")
    max_tokens = int(cfg.get("weekly_planner_max_tokens", 1000))

    tasks_text = "\n".join(
        f"- [{t.get('priority','')}] [{t.get('status','')}] {t['task_name']}"
        for t in project_tasks[:30]
    ) or "(no tasks)"

    user_msg = (
        f"Project: {project['name']}\n"
        f"Goal: {project.get('goal','')}\n"
        f"Current Focus: {project.get('current_focus','')}\n\n"
        f"Project context:\n{project.get('full_text','')[:1500]}\n\n"
        f"Unprocessed ideas:\n{project.get('unprocessed_tasks','') or '(none)'}\n\n"
        f"All tasks for this project:\n{tasks_text}\n\n"
        f"This week's accomplishments:\n{week_summary}\n\n"
        f"General context (time split):\n{general_context[:400]}\n\n"
        f"Plan the next week ({next_week_label}) for this project only. "
        "Generate 3-5 broad weekly goals."
    )

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=_PROJECT_PLAN_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = msg.content[0].text.strip()
    log.debug(
        "Project plan %r: %d in / %d out tokens",
        project["name"], msg.usage.input_tokens, msg.usage.output_tokens,
    )

    try:
        from utils.cost_logger import log_api_call
        log_api_call(model, msg.usage.input_tokens, msg.usage.output_tokens, call_type="weekly_project_plan")
    except Exception:
        pass

    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


# ── Main orchestrator ─────────────────────────────────────────────────────────

def run_weekly_planning(
    week_start: datetime.date,
    general_context: str,
    week_tasks: list[dict],
    all_tasks: list[dict],
    project_pages: list[dict],
) -> dict:
    """
    Run full weekly planning pipeline.

    Steps:
      1. Load day summaries for Mon-Sat of *week_start* week
      2. One LLM call to summarize the week
      3. One LLM call per active project to generate weekly goals
      4. Return assembled plan dict (weekly_store schema)

    Parameters
    ----------
    week_start     : Monday of the week being summarized.
    general_context: From context/fetch_context.load_general_context().
    week_tasks     : From collectors/collect_tasks.get_this_week_tasks().
    all_tasks      : From collectors/collect_tasks.get_all_tasks().
    project_pages  : From collectors/collect_project_pages.get_all_project_pages().
    """
    iso = week_start.isocalendar()
    week_label = f"{iso.year}-W{iso.week:02d}"
    next_week_start = week_start + datetime.timedelta(weeks=1)
    next_iso = next_week_start.isocalendar()
    next_week_label = f"{next_iso.year}-W{next_iso.week:02d}"
    week_end = week_start + datetime.timedelta(days=6)

    log.info("=== Weekly planning: %s ===", week_label)

    # Step 1: Load day summaries
    day_summaries = load_week_day_summaries(week_start)
    log.info("Day summaries: %d days with data", len(day_summaries))

    # Step 2: Week summary LLM call
    log.info("Calling week summary LLM...")
    try:
        summary_result = _call_week_summary_llm(
            day_summaries, week_tasks, general_context, week_label
        )
    except Exception as exc:
        log.warning("Week summary LLM failed: %s — using fallback", exc)
        summary_result = {
            "week_summary": f"Week {week_label} — summary unavailable.",
            "highlights": [],
        }

    week_summary_text = summary_result.get("week_summary", "")
    highlights = summary_result.get("highlights", [])

    # Step 3: Per-project planning LLM calls
    project_plans: list[dict] = []
    for project in project_pages:
        project_name = project["name"]
        project_tasks = [
            t for t in all_tasks
            if t.get("project", "").strip().lower() == project_name.strip().lower()
        ]
        log.info("Planning project %r (%d tasks)...", project_name, len(project_tasks))
        try:
            plan_result = _call_project_plan_llm(
                project, project_tasks, week_summary_text, general_context, next_week_label
            )
            weekly_goals = plan_result.get("weekly_goals", [])
        except Exception as exc:
            log.warning("Project plan LLM failed for %r: %s", project_name, exc)
            weekly_goals = []

        project_plans.append({
            "project_name":    project_name,
            "project_page_id": project["page_id"],
            "weekly_goals":    weekly_goals,
        })

    return {
        "week":          week_label,
        "week_start":    str(week_start),
        "week_end":      str(week_end),
        "generated_at":  datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds"),
        "week_summary":  week_summary_text,
        "highlights":    highlights,
        "projects":      project_plans,
    }
