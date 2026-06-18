"""
One-time setup: create the "General Context" sub-page inside the context page.

Idempotent — prints a warning and exits cleanly if the page already exists.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.notion_client import get_notion

GENERAL_CONTEXT_TITLE = "General Context"


def _rich_text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content}}]


def _h1(text: str) -> dict:
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": _rich_text(text)}}


def _h2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich_text(text)}}


def _h3(text: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": _rich_text(text)}}


def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(text)}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich_text(text)}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _find_existing(ctx_page_id: str) -> str | None:
    notion = get_notion()
    cursor: str | None = None
    while True:
        kwargs: dict = {"block_id": ctx_page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.blocks.children.list(**kwargs)
        for b in resp.get("results", []):
            if b.get("type") == "child_page":
                title = b.get("child_page", {}).get("title", "")
                if GENERAL_CONTEXT_TITLE in title:
                    return b["id"]
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return None


def _template_blocks() -> list[dict]:
    return [
        _h2("About Me"),
        _para("[Your role, background, and current stage — edit this in Notion]"),
        _divider(),
        _h2("Active Projects"),
        _h3("Project 1 — [Name]"),
        _para("Status: [e.g. Validation / Building / Paused]"),
        _para("Goal: [One sentence describing what success looks like]"),
        _para("Current focus: [What you're actively working on right now]"),
        _divider(),
        _h3("Project 2 — [Name]"),
        _para("Status:"),
        _para("Goal:"),
        _para("Current focus:"),
        _divider(),
        _h2("This Week's Goals"),
        _bullet("[Goal 1]"),
        _bullet("[Goal 2]"),
        _divider(),
        _h2("Notes"),
        _para("[Any other background context]"),
        _divider(),
        _para("— Last updated by agent: [never]"),
    ]


_ACCESS_HINT = (
    "\n⚠️  Notion 404: the integration lacks access to the context page.\n"
    "   Fix: open 'Daily Plans - Plan VS Actual' in Notion → ··· → Connections\n"
    "   → add your integration (transcript_automation).\n"
    "   Then run this script again."
)


def main() -> None:
    cfg = get_config()
    ctx_page_id: str = cfg["notion_context_page_id"]
    notion = get_notion()

    try:
        existing_id = _find_existing(ctx_page_id)
    except Exception as exc:
        if "404" in str(exc) or "Could not find" in str(exc):
            print(_ACCESS_HINT)
        else:
            print(f"Error: {exc}")
        sys.exit(1)

    if existing_id:
        url = f"https://www.notion.so/{existing_id.replace('-', '')}"
        print(f"⚠️  'General Context' sub-page already exists — nothing created.")
        print(f"   URL: {url}")
        return

    try:
        new_page = notion.pages.create(
            parent={"page_id": ctx_page_id},
            icon={"type": "emoji", "emoji": "🧠"},
            properties={
                "title": {"title": _rich_text(GENERAL_CONTEXT_TITLE)}
            },
            children=_template_blocks(),
        )
    except Exception as exc:
        if "404" in str(exc) or "Could not find" in str(exc):
            print(_ACCESS_HINT)
        else:
            print(f"Error creating page: {exc}")
        sys.exit(1)

    page_id: str = new_page["id"]
    url = f"https://www.notion.so/{page_id.replace('-', '')}"
    print(f"✅ Created 'General Context' sub-page.")
    print(f"   URL: {url}")
    print("   Edit it in Notion to fill in your background context.")


if __name__ == "__main__":
    main()
