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
