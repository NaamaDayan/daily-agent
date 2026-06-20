# Daily Planner (Plan B) — Design Spec
_Date: 2026-06-20_

## Overview

Add Stage 6 to the existing 20:00 daily pipeline (`run_daily.py`). Stage 6 generates a per-project daily plan for tomorrow and sends it as a second Telegram message, immediately after the existing summary message. No approval flow — informational only.

---

## Architecture

Stage 6 runs after Stage 5 (Deliver) in all modes (live and dry-run). It is non-fatal: a failure logs an error and optionally sends an error Telegram message, but does not change the pipeline exit code.

```
Stage 6: Daily Plan
  6A. load_weekly_plan   weekly_store.get_current_weekly_plan()
                         → None? send/print "Weekly plan not found" and return
  6B. collect projects   collect_project_pages.get_all_project_pages()
  6C. collect tasks      collect_tasks.get_all_tasks() (new function)
  6D. collect calendar   collectors/collect_calendar.get_tomorrow_events() → [] (stub)
  6E. per-project LLM    pipeline/daily_planner.plan_tomorrow_for_project(...)
  6F. send / print       delivery/telegram_daily_plan.send_daily_plan(...)
```

Project matching: weekly plan entries are matched to active projects by `project_name` (case-insensitive string compare against `get_all_project_pages()[].name`). Projects with no weekly goal entry are skipped.

---

## New Files

### `collectors/collect_calendar.py`
Stub Google Calendar collector. Returns `[]` always until wired up.

```python
def get_tomorrow_events() -> list[dict]:
    """Returns [] until Google Calendar credentials are configured."""
    return []
```

Future schema when implemented:
```json
[{"title": "...", "start_time": "09:00", "end_time": "10:00", "duration_minutes": 60}]
```

### `pipeline/daily_planner.py`
Per-project LLM planning. One `messages.create` call per active project that has a matching weekly goal.

**Inputs per call:**
- Project page full text + unprocessed tasks
- Weekly goals for this project (`weekly_goals[].goal/rationale/priority`)
- All non-Done tasks for this project (from `get_all_tasks()`)
- Today's summary: `day_theme`, `done`, `unfinished`
- Tomorrow's calendar events
- General context (time-split rule, etc.)
- Detail level: `low | medium | high` (from `DAILY_PLAN_DETAIL` env var, default `medium`)

**Output per call:**
```json
{"tasks": ["task 1", "task 2"]}
```

Plain task strings only (no priority). Total ~5 tasks across all projects.

**Detail levels** — controls what each task string contains:
- `low`: task name only — `"Add feature X"`
- `medium`: task name + one-line DOD — `"Add feature X — write + pass tests"`
- `high`: task name + DOD + rationale + time estimate — `"Add feature X — write + pass tests (why: critical path, ~90 min)"`

**Public API:**
```python
def plan_tomorrow(
    projects: list[dict],          # from get_all_project_pages()
    all_tasks: list[dict],         # from get_all_tasks()
    weekly_plan: dict,             # from get_current_weekly_plan()
    today_result: dict,            # day_theme, done, unfinished from summarizer
    calendar_events: list[dict],   # from collect_calendar
    general_context: str,
    detail: str = "medium",
) -> list[dict]:                   # [{"project": str, "tasks": [str, ...]}]
```

### `delivery/telegram_daily_plan.py`
Formats and sends the plan message.

**Message format:**
```
📅 *Plan for tomorrow — Mon 22 Jun*

*Daily Agent:*
1\. Add feature X
2\. Make sure tests pass

*Time Tracker:*
1\. Design export UI
```

No-plan fallback:
```
⚠️ Weekly plan not found — skipping daily plan\.
```

**Public API:**
```python
def format_daily_plan_message(plan: list[dict], tomorrow: datetime.date) -> str: ...
def send_daily_plan(plan: list[dict], tomorrow: datetime.date) -> None: ...
```

---

## Modified Files

### `collectors/collect_tasks.py`
Add new function:
```python
def get_all_tasks(project_name: str | None = None) -> list[dict]:
    """
    Return all non-Done tasks from the Tasks DB.
    If project_name is given, filter to tasks belonging to that project (case-insensitive).
    """
```

### `pipeline/run_daily.py`
Add `_plan_tomorrow()` helper called at the end of `run()`, after the existing deliver step, in both live and dry-run modes.

```python
def _plan_tomorrow(result: dict, date: datetime.date, data: dict, dry_run: bool) -> None:
    """Stage 6: generate and send/print the daily plan for tomorrow."""
    ...
```

### `config.yaml`
Add:
```yaml
daily_planner_model: "claude-sonnet-4-6"
daily_planner_max_tokens: 1000
```

---

## Error Handling

| Failure | Behavior |
|---|---|
| No weekly plan | Send/print "not found" message, return early — not a pipeline error |
| Per-project LLM failure | Log warning, skip that project, continue |
| `get_all_project_pages()` failure | Log warning, proceed with empty project context |
| `get_all_tasks()` failure | Log warning, proceed without task context |
| Send failure | Log error, non-fatal (exit code unchanged) |

---

## Testing

New tests in `tests/test_daily_plan.py` (all external calls mocked):

- **`daily_planner.py`**: LLM called once per project with weekly goal match; unmatched projects skipped; one project LLM failure doesn't abort others; `detail` level is passed into prompt
- **`telegram_daily_plan.py`**: correct project grouping; low/medium/high detail formatting; "not found" fallback message; MarkdownV2 escaping
- **`collect_calendar.py`**: `get_tomorrow_events()` returns `[]`
- **`collect_tasks.get_all_tasks()`**: returns all non-Done tasks; filters correctly by project name; case-insensitive match

---

## Assumptions

- Plan A (weekly planner) is deployed and `get_current_weekly_plan()` works before Plan B runs
- Weekly plan schema is stable: `projects[].project_name`, `projects[].weekly_goals[].goal/rationale/priority`
- Google Calendar integration is out of scope for Plan B; `collect_calendar` is a stub
- ~5 tasks total across all active projects (not 5 per project)
- No Notion writes — plan is informational only; approval flow deferred to future

---

## Explicitly Deferred

- Approval flow (user edits plan via Telegram → tasks written to Notion)
- Google Calendar wiring
- Mentor planner (startup/networking goals)
- Learning loop (adapt based on completion rate)
