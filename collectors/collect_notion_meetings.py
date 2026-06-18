"""
Notion meetings collector.

Queries the Research database for pages with Type = "Conversation" on a given
date, extracts only the סיכום section from each page, and returns a clean list.

Real database (discovered by probing):
  cbed3950ad73499d88441be4f7e21286  (Research DB in company-brain)
  Properties: Title (title), Type (select), Date (date), Tags, Link, Related Idea
  Content layout per page:
      ## סיכום
      ## Key points        ← sub-heading, included in summary
      ...bullet points...
      ## Conclusions       ← sub-heading, included in summary
      ...bullet points...
      ## תמלול מלא        ← STOP here; do not include transcript
      ...
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger
from utils.notion_client import get_notion

log = get_logger("collect_notion_meetings")

# ── Notion block helpers ──────────────────────────────────────────────────────

_HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}
_TEXT_TYPES = {"paragraph", "bulleted_list_item", "numbered_list_item",
               "quote", "callout", "toggle"}

SIKHUM_HEADING  = "סיכום"
TAMLUL_HEADING  = "תמלול מלא"


def _plain_text(block: dict) -> str:
    """Concatenate plain_text from all rich_text spans of a block."""
    btype = block.get("type", "")
    content = block.get(btype, {})
    return "".join(
        span.get("plain_text", "")
        for span in content.get("rich_text", [])
    )


def _fetch_all_blocks(page_id: str) -> list[dict]:
    """Fetch ALL block children, handling Notion's 100-result pagination."""
    notion = get_notion()
    blocks: list[dict] = []
    cursor: str | None = None
    while True:
        kwargs: dict = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.blocks.children.list(**kwargs)
        blocks.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return blocks


def _blocks_to_text(blocks: list[dict]) -> str:
    """Convert a list of blocks to a plain-text string."""
    lines: list[str] = []
    numbered_counter = 0
    for block in blocks:
        btype = block.get("type", "")
        text = _plain_text(block).strip()
        if not text and btype not in ("divider",):
            continue
        if btype in _HEADING_TYPES:
            numbered_counter = 0
            lines.append(f"## {text}")
        elif btype == "bulleted_list_item":
            numbered_counter = 0
            lines.append(f"• {text}")
        elif btype == "numbered_list_item":
            numbered_counter += 1
            lines.append(f"{numbered_counter}. {text}")
        elif btype == "divider":
            numbered_counter = 0
            lines.append("---")
        else:
            numbered_counter = 0
            lines.append(text)
    return "\n".join(lines)


# ── Core extraction ──────────────────────────────────────────────────────────

def extract_sikhum(page_id: str) -> str:
    """
    Pure function: fetch a page's blocks and return only the סיכום section text.

    Extraction algorithm:
      1. Find first heading whose plain_text == "סיכום"
      2. Collect all blocks (including sub-headings) until a heading whose
         plain_text == "תמלול מלא" or end-of-page
      3. Fallback: if no סיכום heading found, return first 500 chars of full text
    """
    try:
        blocks = _fetch_all_blocks(page_id)
    except Exception as exc:
        log.warning("Failed to fetch blocks for page %s: %s", page_id, exc)
        return ""

    # ── locate סיכום heading ─────────────────────────────────────────────────
    sikhum_idx: int | None = None
    for i, block in enumerate(blocks):
        if block.get("type") in _HEADING_TYPES and _plain_text(block).strip() == SIKHUM_HEADING:
            sikhum_idx = i
            break

    if sikhum_idx is None:
        # Fallback: first 500 chars of all text content
        log.warning("No סיכום heading found in page %s — using first 500 chars", page_id)
        full = _blocks_to_text(blocks)
        return full[:500]

    # ── collect blocks from sikhum_idx+1 until תמלול מלא ───────────────────
    section_blocks: list[dict] = []
    for block in blocks[sikhum_idx + 1:]:
        if block.get("type") in _HEADING_TYPES and _plain_text(block).strip() == TAMLUL_HEADING:
            break
        section_blocks.append(block)

    return _blocks_to_text(section_blocks)


# ── Date / time helpers ──────────────────────────────────────────────────────

def _time_from_date_prop(date_prop: dict | None) -> str:
    """Return HH:MM if the Date property includes a time component, else ''."""
    if not date_prop or not date_prop.get("date"):
        return ""
    start: str = date_prop["date"].get("start", "") or ""
    if "T" in start:
        return start[11:16]  # slice HH:MM from "2026-05-29T10:30:00.000+03:00"
    return ""


# ── DB query with pagination ─────────────────────────────────────────────────

def _query_db(date_str: str) -> list[dict]:
    """
    Query meetings DB for Type=Conversation AND Date=date_str.
    Handles Notion's 100-result pagination.
    """
    notion = get_notion()
    cfg = get_config()
    db_id: str = cfg["notion_meetings_db_id"]

    compound_filter = {
        "and": [
            {"property": "Date",   "date":   {"equals": date_str}},
            {"property": "Type",   "select": {"equals": "Conversation"}},
        ]
    }

    pages: list[dict] = []
    cursor: str | None = None
    while True:
        kwargs: dict = {
            "database_id": db_id,
            "filter": compound_filter,
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        pages.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    return pages


# ── Public API ───────────────────────────────────────────────────────────────

def get_date(d: datetime.date) -> list[dict]:
    """Return meetings with Type=Conversation on date *d*."""
    date_str = d.isoformat()
    try:
        pages = _query_db(date_str)
    except Exception as exc:
        log.warning("Notion DB query failed for %s: %s", date_str, exc)
        return []

    results: list[dict] = []
    for page in pages:
        page_id: str = page["id"]
        props = page.get("properties", {})

        # Title
        title_parts = props.get("Title", {}).get("title", [])
        title = "".join(t.get("plain_text", "") for t in title_parts).strip()

        # Time from Date property
        date_prop = props.get("Date")
        time_str = _time_from_date_prop(date_prop)

        # Summary — סיכום section only
        try:
            summary = extract_sikhum(page_id)
        except Exception as exc:
            log.warning("Skipping page %s (%s): %s", page_id, title, exc)
            continue

        results.append({"title": title, "summary": summary, "time": time_str})
        log.debug("Collected meeting: %s (%d chars)", title, len(summary))

    log.info("get_date(%s): %d meeting(s) found", date_str, len(results))
    return results


def get_today() -> list[dict]:
    return get_date(datetime.date.today())


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Notion meetings collector")
    parser.add_argument("--date", default=str(datetime.date.today()),
                        help="YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary table")
    args = parser.parse_args()

    try:
        d = datetime.date.fromisoformat(args.date)
    except ValueError:
        print(f"Invalid date: {args.date!r}", file=sys.stderr)
        sys.exit(1)

    meetings = get_date(d)

    if not meetings:
        print(f"No Conversation-type meetings found for {d}.")
        return

    if args.dry_run:
        from rich.console import Console
        from rich.table import Table

        table = Table(title=f"Meetings — {d}  ({len(meetings)} rows)")
        table.add_column("Title", style="cyan", no_wrap=False, max_width=30)
        table.add_column("Time", style="yellow", width=6)
        table.add_column("Summary preview (80 chars)", no_wrap=False, max_width=50)
        table.add_column("Chars", justify="right", style="dim")

        for m in meetings:
            preview = m["summary"][:80].replace("\n", " ")
            table.add_row(m["title"], m["time"] or "—", preview, str(len(m["summary"])))

        Console().print(table)
    else:
        import json
        print(json.dumps(meetings, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
