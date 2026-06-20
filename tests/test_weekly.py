"""Tests for weekly pipeline modules (all external calls mocked)."""
from __future__ import annotations
import datetime
import json
import pytest
from unittest.mock import patch, MagicMock


# ── Task 1: collect_project_pages ─────────────────────────────────────────

class TestGetAllProjectPages:
    def _make_project(self, page_id="pid1", name="Daily Agent"):
        return {"page_id": page_id, "name": name, "goal": "g", "current_focus": "c", "priority": "P1 High", "status": "🟢 Active"}

    def _make_block(self, btype, text, checked=False):
        block = {"type": btype, btype: {"rich_text": [{"plain_text": text}]}}
        if btype == "to_do":
            block[btype]["checked"] = checked
        return block

    @patch("collectors.collect_project_pages.get_active_projects")
    @patch("collectors.collect_project_pages._fetch_all_blocks")
    def test_returns_augmented_projects(self, mock_blocks, mock_projects):
        mock_projects.return_value = [self._make_project()]
        heading = {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Unprocessed Tasks"}]}}
        task_block = self._make_block("bulleted_list_item", "Add dark mode")
        mock_blocks.return_value = [heading, task_block]

        from collectors.collect_project_pages import get_all_project_pages
        result = get_all_project_pages()

        assert len(result) == 1
        p = result[0]
        assert p["name"] == "Daily Agent"
        assert "Add dark mode" in p["unprocessed_tasks"]
        assert isinstance(p["full_text"], str)

    @patch("collectors.collect_project_pages.get_active_projects")
    @patch("collectors.collect_project_pages._fetch_all_blocks")
    def test_missing_section_returns_empty_string(self, mock_blocks, mock_projects):
        mock_projects.return_value = [self._make_project()]
        mock_blocks.return_value = [self._make_block("paragraph", "Some text")]

        from collectors.collect_project_pages import get_all_project_pages
        result = get_all_project_pages()

        assert result[0]["unprocessed_tasks"] == ""

    @patch("collectors.collect_project_pages.get_active_projects")
    @patch("collectors.collect_project_pages._fetch_all_blocks")
    def test_notion_error_returns_empty_fields(self, mock_blocks, mock_projects):
        mock_projects.return_value = [self._make_project()]
        mock_blocks.side_effect = Exception("Notion 404")

        from collectors.collect_project_pages import get_all_project_pages
        result = get_all_project_pages()

        assert result[0]["full_text"] == ""
        assert result[0]["unprocessed_tasks"] == ""

    @patch("collectors.collect_project_pages.get_active_projects")
    def test_no_projects_returns_empty_list(self, mock_projects):
        mock_projects.return_value = []

        from collectors.collect_project_pages import get_all_project_pages
        assert get_all_project_pages() == []


# ── Task 2: get_all_tasks ────────────────────────────────────────────────────

class TestGetAllTasks:
    @patch("collectors.collect_tasks.get_notion")
    def test_returns_all_statuses(self, mock_get_notion):
        mock_notion = MagicMock()
        mock_get_notion.return_value = mock_notion
        mock_notion.databases.query.return_value = {
            "results": [
                {
                    "id": "page1",
                    "properties": {
                        "Task Name": {"type": "title", "title": [{"plain_text": "Write tests"}]},
                        "Status": {"type": "select", "select": {"name": "📋 Backlog"}},
                        "Priority": {"type": "select", "select": {"name": "P2 Medium"}},
                        "Project": {"type": "relation", "relation": []},
                        "Estimated Duration": {"type": "number", "number": None},
                        "Notes": {"type": "rich_text", "rich_text": []},
                        "Target Count": {"type": "number", "number": None},
                        "Recurrence": {"type": "select", "select": None},
                        "Scheduled Date": {"type": "date", "date": None},
                    }
                }
            ],
            "has_more": False,
        }

        from collectors.collect_tasks import get_all_tasks
        result = get_all_tasks()

        assert len(result) == 1
        assert result[0]["task_name"] == "Write tests"
        assert result[0]["status"] == "📋 Backlog"

    @patch("collectors.collect_tasks.get_notion")
    def test_returns_empty_on_api_error(self, mock_get_notion):
        mock_get_notion.side_effect = Exception("timeout")

        from collectors.collect_tasks import get_all_tasks
        assert get_all_tasks() == []


# ── Task 3: weekly_store ───────────────────────────────────────────────────────

class TestWeeklyStore:
    WEEK_START = datetime.date(2026, 6, 15)
    SAMPLE_PLAN = {
        "week": "2026-W25",
        "week_start": "2026-06-15",
        "week_end": "2026-06-21",
        "generated_at": "2026-06-20T21:00:00+00:00",
        "week_summary": "Good week.",
        "highlights": ["Shipped weekly pipeline"],
        "projects": [
            {
                "project_name": "Daily Agent",
                "project_page_id": "pid1",
                "weekly_goals": [
                    {"goal": "Complete approval flow", "rationale": "Core UX", "priority": "high"}
                ],
            }
        ],
    }

    def test_save_and_load_roundtrip(self, tmp_path):
        with patch("pipeline.weekly_store._weekly_plans_dir", return_value=tmp_path):
            from pipeline.weekly_store import save_weekly_plan, load_weekly_plan
            save_weekly_plan(self.SAMPLE_PLAN, self.WEEK_START)
            loaded = load_weekly_plan(self.WEEK_START)

        assert loaded is not None
        assert loaded["week"] == "2026-W25"
        assert loaded["projects"][0]["project_name"] == "Daily Agent"

    def test_load_missing_returns_none(self, tmp_path):
        with patch("pipeline.weekly_store._weekly_plans_dir", return_value=tmp_path):
            from pipeline.weekly_store import load_weekly_plan
            result = load_weekly_plan(self.WEEK_START)
        assert result is None

    def test_get_current_weekly_plan_returns_latest(self, tmp_path):
        with patch("pipeline.weekly_store._weekly_plans_dir", return_value=tmp_path):
            from pipeline.weekly_store import save_weekly_plan, get_current_weekly_plan
            save_weekly_plan(self.SAMPLE_PLAN, self.WEEK_START)
            result = get_current_weekly_plan()
        assert result is not None
        assert result["week"] == "2026-W25"
