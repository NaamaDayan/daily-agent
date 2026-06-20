"""
Project page content collector.

Fetches the full Notion page content for each active project, extracting:
  - full_text: entire page as plain text (max 3000 chars)
  - unprocessed_tasks: content under the "Unprocessed Tasks" heading

Public API
----------
get_all_project_pages() -> list[dict]
    Returns active projects augmented with page content fields.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from collectors.collect_tasks import get_active_projects
from context.fetch_context import (
    _fetch_all_blocks,
    _blocks_to_plaintext,
    _extract_section,
)
from utils.logger import get_logger

log = get_logger("collect_project_pages")

_MAX_FULL_TEXT_CHARS = 3000
_UNPROCESSED_HEADING = "Unprocessed Tasks"


def _get_project_page_content(page_id: str) -> dict:
    """Fetch and extract content from a single project Notion page."""
    blocks = _fetch_all_blocks(page_id)
    full_text = _blocks_to_plaintext(blocks, max_chars=_MAX_FULL_TEXT_CHARS)
    unprocessed_tasks = _extract_section(
        blocks,
        heading_text=_UNPROCESSED_HEADING,
        stop_headings={"Plan", "Actual", "Notes", "Background"},
    )
    return {"full_text": full_text, "unprocessed_tasks": unprocessed_tasks}


def get_all_project_pages() -> list[dict]:
    """
    Return all active projects augmented with their Notion page content.

    Each dict has all fields from get_active_projects() plus:
      full_text: str          — page as plain text, max 3000 chars
      unprocessed_tasks: str  — "Unprocessed Tasks" section, "" if absent

    Per-project Notion errors are isolated; the project is included with
    empty content fields rather than dropped.
    """
    projects = get_active_projects()
    result: list[dict] = []

    for project in projects:
        page_id = project["page_id"]
        content = {"full_text": "", "unprocessed_tasks": ""}
        try:
            content = _get_project_page_content(page_id)
            log.debug(
                "Project %r: %d chars, unprocessed=%d chars",
                project["name"],
                len(content["full_text"]),
                len(content["unprocessed_tasks"]),
            )
        except Exception as exc:
            log.warning("Failed to fetch page for project %r: %s", project["name"], exc)

        result.append({**project, **content})

    return result
