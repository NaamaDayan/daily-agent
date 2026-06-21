"""Tests for Plan B daily planner feature (all external calls mocked)."""
from __future__ import annotations
import datetime
import json
import pytest
from unittest.mock import patch, MagicMock


# ── Task 1: get_all_tasks ─────────────────────────────────────────────────────

def _make_page(task_name: str, status: str, project: str) -> dict:
    """Helper: build a minimal Notion Tasks page dict."""
    return {
        "id": f"page-{task_name[:8]}",
        "properties": {
            "Task Name": {"title": [{"plain_text": task_name}]},
            "Status":    {"select": {"name": status}},
            "Priority":  {"select": {"name": "P2 Medium"}},
            "Project":   {"relation": []},
            "Estimated Duration": {"number": None},
            "Notes":     {"rich_text": []},
            "Target Count": {"number": None},
            "Recurrence":   {"select": None},
        },
    }


class TestGetAllTasks:
    @patch("collectors.collect_tasks._query_all")
    @patch("collectors.collect_tasks._page_to_task")
    def test_returns_all_non_done_tasks(self, mock_p2t, mock_query):
        mock_query.return_value = [_make_page("Fix bug", "🎯 Today", "")]
        mock_p2t.return_value = {
            "page_id": "p1", "task_name": "Fix bug", "project": "",
            "status": "🎯 Today", "priority": "P2 Medium",
            "estimated_minutes": None, "notes": "", "target_count": 1, "recurrence": "None",
        }

        from collectors.collect_tasks import get_all_tasks
        result = get_all_tasks()

        assert len(result) == 1
        assert result[0]["task_name"] == "Fix bug"
        # Verify filter excludes Done
        called_filter = mock_query.call_args[0][1]
        assert called_filter["property"] == "Status"
        assert called_filter["select"]["does_not_equal"] == "✅ Done"

    @patch("collectors.collect_tasks._query_all")
    @patch("collectors.collect_tasks._page_to_task")
    def test_filters_by_project_name(self, mock_p2t, mock_query):
        mock_query.return_value = [_make_page("Task A", "🎯 Today", ""), _make_page("Task B", "🎯 Today", "")]
        mock_p2t.side_effect = [
            {"page_id": "p1", "task_name": "Task A", "project": "Daily Agent",
             "status": "🎯 Today", "priority": "P2 Medium",
             "estimated_minutes": None, "notes": "", "target_count": 1, "recurrence": "None"},
            {"page_id": "p2", "task_name": "Task B", "project": "Time Tracker",
             "status": "🎯 Today", "priority": "P2 Medium",
             "estimated_minutes": None, "notes": "", "target_count": 1, "recurrence": "None"},
        ]

        from collectors.collect_tasks import get_all_tasks
        result = get_all_tasks(project_name="Daily Agent")

        assert len(result) == 1
        assert result[0]["project"] == "Daily Agent"

    @patch("collectors.collect_tasks._query_all")
    @patch("collectors.collect_tasks._page_to_task")
    def test_project_name_case_insensitive(self, mock_p2t, mock_query):
        mock_query.return_value = [_make_page("Task A", "🎯 Today", "")]
        mock_p2t.return_value = {
            "page_id": "p1", "task_name": "Task A", "project": "Daily Agent",
            "status": "🎯 Today", "priority": "P2 Medium",
            "estimated_minutes": None, "notes": "", "target_count": 1, "recurrence": "None",
        }

        from collectors.collect_tasks import get_all_tasks
        result = get_all_tasks(project_name="daily agent")
        assert len(result) == 1

    @patch("collectors.collect_tasks._query_all", side_effect=Exception("Notion down"))
    def test_notion_failure_returns_empty_list(self, _):
        from collectors.collect_tasks import get_all_tasks
        result = get_all_tasks()
        assert result == []


# ── Task 2: collect_calendar ──────────────────────────────────────────────────

class TestCollectCalendar:
    def test_returns_empty_list(self):
        from collectors.collect_calendar import get_tomorrow_events
        result = get_tomorrow_events()
        assert result == []

    def test_returns_list_type(self):
        from collectors.collect_calendar import get_tomorrow_events
        result = get_tomorrow_events()
        assert isinstance(result, list)


# ── Task 3: daily_planner ─────────────────────────────────────────────────────

def _make_project_page(name: str, page_id: str = "pid1") -> dict:
    return {
        "page_id": page_id, "name": name, "goal": "ship it",
        "current_focus": "focus", "priority": "P1 High", "status": "🟢 Active",
        "full_text": "Project context here.", "unprocessed_tasks": "- idea 1",
    }


def _make_weekly_plan(project_name: str) -> dict:
    return {
        "projects": [
            {
                "project_name": project_name,
                "project_page_id": "pid1",
                "weekly_goals": [
                    {"goal": "Finish planner", "rationale": "critical", "priority": "high"},
                ],
            }
        ]
    }


def _make_today_result() -> dict:
    return {
        "day_theme": "Built timeline module",
        "done": [{"task_name": "Write tests", "project": "Daily Agent"}],
        "unfinished": [],
    }


class TestDailyPlanner:
    @patch("pipeline.daily_planner._call_project_daily_llm")
    def test_calls_llm_once_per_matched_project(self, mock_llm):
        mock_llm.return_value = ["Add feature X", "Write tests"]

        from pipeline.daily_planner import plan_tomorrow
        result = plan_tomorrow(
            projects=[_make_project_page("Daily Agent")],
            all_tasks=[],
            weekly_plan=_make_weekly_plan("Daily Agent"),
            today_result=_make_today_result(),
            calendar_events=[],
            general_context="Work 9-18",
            detail="medium",
        )

        assert mock_llm.call_count == 1
        assert len(result) == 1
        assert result[0]["project"] == "Daily Agent"
        assert result[0]["tasks"] == ["Add feature X", "Write tests"]

    @patch("pipeline.daily_planner._call_project_daily_llm")
    def test_unmatched_projects_skipped(self, mock_llm):
        mock_llm.return_value = ["Task 1"]

        from pipeline.daily_planner import plan_tomorrow
        result = plan_tomorrow(
            projects=[_make_project_page("Other Project")],
            all_tasks=[],
            weekly_plan=_make_weekly_plan("Daily Agent"),
            today_result=_make_today_result(),
            calendar_events=[],
            general_context="",
            detail="medium",
        )

        assert mock_llm.call_count == 0
        assert result == []

    @patch("pipeline.daily_planner._call_project_daily_llm")
    def test_llm_failure_continues_other_projects(self, mock_llm):
        mock_llm.side_effect = [Exception("API error"), ["Task from proj2"]]

        from pipeline.daily_planner import plan_tomorrow
        result = plan_tomorrow(
            projects=[
                _make_project_page("Daily Agent", "pid1"),
                _make_project_page("Time Tracker", "pid2"),
            ],
            all_tasks=[],
            weekly_plan={
                "projects": [
                    {"project_name": "Daily Agent", "project_page_id": "pid1",
                     "weekly_goals": [{"goal": "G1", "rationale": "", "priority": "high"}]},
                    {"project_name": "Time Tracker", "project_page_id": "pid2",
                     "weekly_goals": [{"goal": "G2", "rationale": "", "priority": "medium"}]},
                ]
            },
            today_result=_make_today_result(),
            calendar_events=[],
            general_context="",
            detail="medium",
        )

        assert mock_llm.call_count == 2
        assert len(result) == 1
        assert result[0]["project"] == "Time Tracker"

    @patch("pipeline.daily_planner._call_project_daily_llm")
    def test_detail_level_passed_to_llm(self, mock_llm):
        mock_llm.return_value = ["Task 1"]

        from pipeline.daily_planner import plan_tomorrow
        plan_tomorrow(
            projects=[_make_project_page("Daily Agent")],
            all_tasks=[],
            weekly_plan=_make_weekly_plan("Daily Agent"),
            today_result=_make_today_result(),
            calendar_events=[],
            general_context="",
            detail="high",
        )

        call_kwargs = mock_llm.call_args
        assert call_kwargs.kwargs.get("detail") == "high"


# ── Task 4: telegram_daily_plan ───────────────────────────────────────────────

class TestTelegramDailyPlan:
    TOMORROW = datetime.date(2026, 6, 21)

    def _sample_plan(self) -> list[dict]:
        return [
            {"project": "Daily Agent", "tasks": ["Add feature X", "Write tests"]},
            {"project": "Time Tracker", "tasks": ["Design export UI"]},
        ]

    def test_format_contains_project_names(self):
        from delivery.telegram_daily_plan import format_daily_plan_message
        msg = format_daily_plan_message(self._sample_plan(), self.TOMORROW)
        assert "Daily Agent" in msg
        assert "Time Tracker" in msg

    def test_format_contains_tasks(self):
        from delivery.telegram_daily_plan import format_daily_plan_message
        msg = format_daily_plan_message(self._sample_plan(), self.TOMORROW)
        assert "Add feature X" in msg
        assert "Write tests" in msg
        assert "Design export UI" in msg

    def test_format_numbered_list(self):
        from delivery.telegram_daily_plan import format_daily_plan_message
        msg = format_daily_plan_message(self._sample_plan(), self.TOMORROW)
        assert r"1\." in msg
        assert r"2\." in msg

    def test_format_empty_plan_shows_no_projects(self):
        from delivery.telegram_daily_plan import format_daily_plan_message
        msg = format_daily_plan_message([], self.TOMORROW)
        assert "no projects" in msg.lower() or "No projects" in msg

    def test_format_escapes_special_chars(self):
        from delivery.telegram_daily_plan import format_daily_plan_message
        plan = [{"project": "My-Project", "tasks": ["Fix bug (urgent)"]}]
        msg = format_daily_plan_message(plan, self.TOMORROW)
        assert r"\-" in msg or "My" in msg
        assert r"\(" in msg

    def test_no_weekly_plan_message_constant(self):
        from delivery.telegram_daily_plan import NO_WEEKLY_PLAN_MSG
        assert "weekly plan" in NO_WEEKLY_PLAN_MSG.lower()
        assert isinstance(NO_WEEKLY_PLAN_MSG, str)

    @patch("delivery.telegram_daily_plan._send_raw")
    def test_send_daily_plan_calls_send_raw(self, mock_send):
        mock_send.return_value = {"ok": True}
        from delivery.telegram_daily_plan import send_daily_plan
        send_daily_plan(self._sample_plan(), self.TOMORROW)
        assert mock_send.call_count == 1
        assert mock_send.call_args.kwargs.get("parse_mode") == "MarkdownV2"


# ── Task 5: Stage 6 integration in run_daily ─────────────────────────────────

class TestStage6Integration:
    @patch("pipeline.run_daily._plan_tomorrow")
    @patch("pipeline.run_daily._deliver", return_value=True)
    @patch("pipeline.run_daily._write_notion")
    @patch("pipeline.run_daily._save_pending_summary")
    @patch("pipeline.run_daily._store")
    @patch("delivery.telegram_send.format_classification_message", return_value="msg")
    @patch("pipeline.run_daily._summarize")
    @patch("pipeline.run_daily._collect_parallel")
    def test_plan_tomorrow_called_in_live_mode(
        self, mock_collect, mock_summarize, mock_fmt,
        mock_store, mock_save_ps, mock_write_notion, mock_deliver, mock_plan,
    ):
        mock_collect.return_value = {
            "date": datetime.date(2026, 6, 20),
            "context": {"general": "Work 9-18", "today": None, "active_projects": [], "today_tasks": []},
        }
        mock_summarize.return_value = {
            "day_theme": "Built stuff", "done": [], "unfinished": [],
            "unclassified_activities": [], "time_breakdown": [], "unmatched_segments": [],
            "today_tasks": [],
        }

        from pipeline.run_daily import run
        run(datetime.date(2026, 6, 20))

        mock_plan.assert_called_once()
        assert mock_plan.call_args.kwargs.get("dry_run") is False

    @patch("pipeline.run_daily._plan_tomorrow")
    @patch("delivery.telegram_send.format_classification_message", return_value="msg")
    @patch("pipeline.run_daily._summarize")
    @patch("pipeline.run_daily._collect_parallel")
    def test_plan_tomorrow_called_in_dry_run(
        self, mock_collect, mock_summarize, mock_fmt, mock_plan,
    ):
        mock_collect.return_value = {
            "date": datetime.date(2026, 6, 20),
            "context": {"general": "", "today": None, "active_projects": [], "today_tasks": []},
        }
        mock_summarize.return_value = {
            "day_theme": "Built stuff", "done": [], "unfinished": [],
            "unclassified_activities": [], "time_breakdown": [], "unmatched_segments": [],
            "today_tasks": [],
        }

        from pipeline.run_daily import run
        run(datetime.date(2026, 6, 20), dry_run=True)

        mock_plan.assert_called_once()
        assert mock_plan.call_args.kwargs.get("dry_run") is True

    @patch("delivery.telegram_send.send_text")
    @patch("pipeline.daily_planner._call_project_daily_llm")
    @patch("pipeline.weekly_store.get_current_weekly_plan")
    def test_no_weekly_plan_sends_fallback_message(
        self, mock_weekly, mock_llm, mock_send_text,
    ):
        mock_weekly.return_value = None

        from pipeline.run_daily import _plan_tomorrow
        _plan_tomorrow(
            {"day_theme": "test", "done": [], "unfinished": []},
            datetime.date(2026, 6, 20),
            {"context": {"general": ""}},
            dry_run=False,
        )

        mock_llm.assert_not_called()
        mock_send_text.assert_called_once()
        sent_text = mock_send_text.call_args[0][0]
        assert "weekly plan" in sent_text.lower()
