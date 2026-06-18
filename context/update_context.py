"""
Context writer.

Updates the "Daily Plans - Plan VS Actual" Notion page.

  update_general_context(text)          → append timestamped paragraph to General Context
  upsert_daily_entry(date, plan, actual) → create or update YYYY-MM-DD sub-page
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

log = get_logger("update_context")

_HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}
GENERAL_CONTEXT_TITLE = "General Context"


# ── Block construction helpers ────────────────────────────────────────────────

def _rich_text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content}}]


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(text)}}


def _heading2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich_text(text)}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich_text(text)}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _text_to_blocks(text: str) -> list[dict]:
    """
    Convert a plain text string to Notion blocks.
    Multi-line → one bulleted_list_item per non-empty line.
    Single line → one paragraph block.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return [_paragraph("")]
    if len(lines) == 1:
        return [_paragraph(lines[0])]
    return [_bullet(ln) for ln in lines]


# ── Shared block fetcher ──────────────────────────────────────────────────────

def _fetch_all_blocks(block_id: str) -> list[dict]:
    notion = get_notion()
    results: list[dict] = []
    cursor: str | None = None
    while True:
        kwargs: dict = {"block_id": block_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.blocks.children.list(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


def _plain_text(block: dict) -> str:
    btype = block.get("type", "")
    return "".join(
        s.get("plain_text", "")
        for s in block.get(btype, {}).get("rich_text", [])
    )


# ── Child page discovery / creation ─────────────────────────────────────────

def _find_child_page(parent_id: str, title: str) -> str | None:
    notion = get_notion()
    blocks = _fetch_all_blocks(parent_id)
    for b in blocks:
        if b.get("type") == "child_page":
            page_title = b.get("child_page", {}).get("title", "")
            if title in page_title or page_title == title:
                return b["id"]
    return None


def _find_or_create_daily_page(date: datetime.date) -> str:
    """Return the page_id for the YYYY-MM-DD sub-page, creating it if necessary."""
    cfg = get_config()
    ctx_page_id: str = cfg["notion_context_page_id"]
    date_str = str(date)

    try:
        existing = _find_child_page(ctx_page_id, date_str)
    except Exception as exc:
        if "404" in str(exc) or "Could not find" in str(exc):
            raise RuntimeError(
                f"Notion 404 — the integration lacks access to the context page.\n"
                "Fix: open 'Daily Plans - Plan VS Actual' in Notion → ··· → Connections\n"
                "→ add your integration, then retry."
            ) from exc
        raise

    if existing:
        log.debug("Found existing daily page for %s: %s", date_str, existing)
        return existing

    # Create the page with full Plan / Actual structure
    notion = get_notion()
    new_page = notion.pages.create(
        parent={"page_id": ctx_page_id},
        icon={"type": "emoji", "emoji": "📅"},
        properties={
            "title": {"title": _rich_text(date_str)}
        },
        children=[
            _heading2("Plan"),
            _bullet("[The agent will write tomorrow's plan here automatically at 20:00]"),
            _bullet("[You can also edit this manually before the day starts]"),
            _bullet("[Format: one task per bullet, keep it concrete]"),
            _heading2("Actual"),
            _paragraph("Written by the agent at end of day — do not edit manually."),
        ],
    )
    page_id: str = new_page["id"]
    log.info("Created daily page for %s: %s", date_str, page_id)
    return page_id


# ── Section update helpers ────────────────────────────────────────────────────

def _find_heading_block(blocks: list[dict], heading_text: str) -> dict | None:
    """Return the first heading block whose plain text matches heading_text."""
    for b in blocks:
        if b.get("type") in _HEADING_TYPES and _plain_text(b).strip() == heading_text:
            return b
    return None


def _blocks_between_headings(blocks: list[dict],
                              start_heading_text: str,
                              stop_heading_texts: set[str]) -> list[dict]:
    """
    Return the (non-heading) blocks between start_heading and the next heading
    whose text is in stop_heading_texts (exclusive of both heading blocks).
    """
    in_section = False
    result: list[dict] = []
    for b in blocks:
        if b.get("type") in _HEADING_TYPES:
            text = _plain_text(b).strip()
            if text == start_heading_text:
                in_section = True
                continue
            if in_section and text in stop_heading_texts:
                break
        elif in_section:
            result.append(b)
    return result


def _replace_section_content(page_id: str,
                              heading_text: str,
                              stop_headings: set[str],
                              new_blocks: list[dict]) -> None:
    """
    Delete existing content blocks in a section and insert new ones after the heading.
    The heading block itself is never deleted.
    """
    notion = get_notion()
    blocks = _fetch_all_blocks(page_id)

    heading_block = _find_heading_block(blocks, heading_text)
    if heading_block is None:
        log.warning("Heading '%s' not found in page %s — appending at end", heading_text, page_id)
        notion.blocks.children.append(block_id=page_id, children=new_blocks)
        return

    # Delete existing content in this section
    old_content = _blocks_between_headings(blocks, heading_text, stop_headings)
    for b in old_content:
        try:
            notion.blocks.delete(block_id=b["id"])
        except Exception as exc:
            log.warning("Failed to delete block %s: %s", b["id"], exc)

    # Append new content after the heading block
    notion.blocks.children.append(
        block_id=page_id,
        after=heading_block["id"],
        children=new_blocks,
    )
    log.debug("Replaced section '%s' with %d new blocks", heading_text, len(new_blocks))


# ── Public API ───────────────────────────────────────────────────────────────

def update_general_context(text: str) -> None:
    """Append a timestamped paragraph to the General Context sub-page."""
    cfg = get_config()
    ctx_page_id: str = cfg["notion_context_page_id"]

    try:
        gc_id = _find_child_page(ctx_page_id, GENERAL_CONTEXT_TITLE)
    except Exception as exc:
        if "404" in str(exc) or "Could not find" in str(exc):
            raise RuntimeError(
                f"Notion 404 — the integration lacks access to the context page.\n"
                "Fix: open 'Daily Plans - Plan VS Actual' in Notion → ··· → Connections\n"
                "→ add your integration, then retry."
            ) from exc
        raise

    if gc_id is None:
        raise RuntimeError(
            f"'General Context' sub-page not found under {ctx_page_id}. "
            "Run context/init_context_page.py first."
        )

    notion = get_notion()
    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds")
    entry = f"[{timestamp}] {text.strip()}"

    notion.blocks.children.append(
        block_id=gc_id,
        children=[_divider(), _paragraph(entry)],
    )
    log.info("Appended to General Context: %s", entry[:80])
    print(f"✓ General Context updated: {entry[:80]}")


def upsert_daily_entry(
    date: datetime.date,
    plan: str | None = None,
    actual: str | None = None,
    done_count: int | None = None,
    total_count: int | None = None,
) -> None:
    """
    Create or update the YYYY-MM-DD sub-page. Replaces the named sections.

    Parameters
    ----------
    plan        : Text to write under the ## Plan heading.
    actual      : Summary text for ## Actual.
    done_count  : If provided (with total_count), appends a task completion
                  line to the actual section: "Tasks: N/M completed".
    total_count : Total tasks for the day (used with done_count).
    """
    if plan is None and actual is None:
        raise ValueError("At least one of plan or actual must be provided")

    page_id = _find_or_create_daily_page(date)

    if plan is not None:
        _replace_section_content(
            page_id,
            heading_text="Plan",
            stop_headings={"Actual"},
            new_blocks=_text_to_blocks(plan),
        )
        log.info("Updated Plan for %s", date)
        print(f"✓ Plan updated for {date}")

    if actual is not None:
        # Optionally append task completion summary
        actual_text = actual
        if done_count is not None and total_count is not None:
            actual_text = f"{actual}\n\nTasks: {done_count}/{total_count} completed"
        _replace_section_content(
            page_id,
            heading_text="Actual",
            stop_headings=set(),
            new_blocks=_text_to_blocks(actual_text),
        )
        log.info("Updated Actual for %s", date)
        print(f"✓ Actual updated for {date}")


# ── EOD task reply processor ─────────────────────────────────────────────────

_TASK_PARSE_SYSTEM = (
    "Parse task update instructions. Output JSON only. No explanation."
)


def process_eod_task_replies(
    reply_text: str,
    today_tasks: list[dict],
) -> dict:
    """
    Parse a natural-language EOD reply and update tasks in the Notion DB.

    Supports replies like:
      "1 2 done, 3 defer to tomorrow"
      "done: 1, 3, 4 — defer 2 to monday"
      "all done"
      "add: review pitch deck as P1 for tomorrow"

    Steps:
      a. Ask Claude (haiku, max_tokens=400) to parse the reply into structured JSON.
      b. Call mark_task_done()    for each 'done' page_id.
      c. Call mark_task_deferred() for each 'deferred' item.
      d. Call create_task()       for each new_task.

    Returns:
      {
        "done_count":     int,
        "deferred_count": int,
        "created_count":  int,
        "done_names":     list[str],
        "deferred_names": list[str],
      }

    Raises RuntimeError on Claude API failure.
    """
    import json
    import anthropic as _anthropic

    from collectors.collect_tasks import mark_task_done, mark_task_deferred, create_task

    cfg = get_config()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    # Build numbered list with page_ids for Claude to reference
    numbered_lines: list[str] = []
    for i, t in enumerate(today_tasks, 1):
        pid  = t["page_id"]
        name = t.get("task_name", "")
        proj = t.get("project", "")
        proj_str = f" ({proj})" if proj else ""
        numbered_lines.append(f"{i}. [{pid}] {name}{proj_str}")
    numbered_list = "\n".join(numbered_lines) if numbered_lines else "(no tasks)"

    user_msg = (
        f"Tasks (numbered list):\n{numbered_list}\n\n"
        f"User instruction: '{reply_text}'\n\n"
        "Return JSON:\n"
        "{\n"
        '  "done": ["page_id", ...],\n'
        '  "deferred": [{"page_id": "...", "to_date": "YYYY-MM-DD"}, ...],\n'
        '  "new_tasks": [{"name": "...", "priority": "P1 High|P2 Medium|P3 Low", '
        '"scheduled_date": "YYYY-MM-DD"}]\n'
        "}\n"
        f"'to_date' defaults to {tomorrow} if not specified.\n"
        "'all done' means all page_ids go in the done list.\n"
        "Return empty lists for categories not mentioned.\n"
        "Use the exact page_id strings from the numbered list above."
    )

    model: str = cfg.get(
        "anthropic_cursor_presummary_model", "claude-haiku-4-5-20251001"
    )
    client = _anthropic.Anthropic(api_key=cfg["anthropic_api_key"])

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=400,
            temperature=0,
            system=_TASK_PARSE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = msg.content[0].text.strip()
        log.debug("EOD parse raw: %s", raw[:300])
        try:
            from utils.cost_logger import log_api_call
            log_api_call(
                model, msg.usage.input_tokens, msg.usage.output_tokens,
                call_type="eod_parse",
            )
        except Exception:
            pass
    except _anthropic.APIError as exc:
        raise RuntimeError(f"Claude API error during EOD parse: {exc}") from exc

    # Strip optional code fences
    if raw.startswith("```"):
        lines = raw.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(raw)
        raw = "\n".join(lines[1:end if end < 0 else None]).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Claude returned non-JSON for EOD parse: {exc}\nRaw: {raw[:200]}"
        ) from exc

    done_ids:     list[str]  = parsed.get("done", [])
    deferred_items: list[dict] = parsed.get("deferred", [])
    new_tasks_raw:  list[dict] = parsed.get("new_tasks", [])

    # Build page_id → task_name map for human-readable summary
    id_to_name = {t["page_id"]: t.get("task_name", t["page_id"][:8])
                  for t in today_tasks}

    # ── Apply done ────────────────────────────────────────────────────────────
    done_names: list[str] = []
    for pid in done_ids:
        try:
            mark_task_done(pid)
            done_names.append(id_to_name.get(pid, pid[:8]))
        except Exception as exc:
            log.warning("mark_task_done failed for %s: %s", pid[:8], exc)

    # ── Apply deferred ────────────────────────────────────────────────────────
    deferred_names: list[str] = []
    for item in deferred_items:
        pid = item.get("page_id", "")
        to_date_str = item.get("to_date", tomorrow)
        try:
            to_date = datetime.date.fromisoformat(to_date_str)
            mark_task_deferred(pid, to_date)
            deferred_names.append(id_to_name.get(pid, pid[:8]))
        except Exception as exc:
            log.warning("mark_task_deferred failed for %s: %s", pid[:8], exc)

    # ── Create new tasks ──────────────────────────────────────────────────────
    created_count = 0
    for nt in new_tasks_raw:
        name      = nt.get("name", "").strip()
        priority  = nt.get("priority", "P2 Medium")
        sched_str = nt.get("scheduled_date", tomorrow)
        if not name:
            continue
        try:
            sched = datetime.date.fromisoformat(sched_str)
            create_task(
                task_name=name,
                status="🎯 Today",
                priority=priority,
                scheduled_date=sched,
            )
            created_count += 1
        except Exception as exc:
            log.warning("create_task failed for %r: %s", name[:40], exc)

    log.info(
        "process_eod_task_replies: done=%d  deferred=%d  created=%d",
        len(done_names), len(deferred_names), created_count,
    )
    return {
        "done_count":     len(done_names),
        "deferred_count": len(deferred_names),
        "created_count":  created_count,
        "done_names":     done_names,
        "deferred_names": deferred_names,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> datetime.date:
    if s.lower() == "today":
        return datetime.date.today()
    return datetime.date.fromisoformat(s)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Agent context writer")
    sub = parser.add_subparsers(dest="cmd")

    # --general TEXT
    g = sub.add_parser("general", help="Append text to General Context")
    g.add_argument("text", help="Text to append")

    # --plan DATE TEXT
    p = sub.add_parser("plan", help="Set plan for a date")
    p.add_argument("date", help="YYYY-MM-DD or 'today'")
    p.add_argument("text", help="Plan text (use \\n for multiple tasks)")

    # --actual DATE TEXT
    a = sub.add_parser("actual", help="Set actual for a date")
    a.add_argument("date", help="YYYY-MM-DD or 'today'")
    a.add_argument("text", help="Actual text")

    # Also support flag-style: --general / --plan / --actual
    # to match the spec's CLI examples
    parser.add_argument("--general", metavar="TEXT", help="Append to General Context")
    parser.add_argument("--plan", nargs=2, metavar=("DATE", "TEXT"),
                        help="Set plan: --plan DATE 'text'")
    parser.add_argument("--actual", nargs=2, metavar=("DATE", "TEXT"),
                        help="Set actual: --actual DATE 'text'")

    args = parser.parse_args()

    # Handle flag-style usage
    if args.general:
        update_general_context(args.general.replace("\\n", "\n"))
        return
    if args.plan:
        date = _parse_date(args.plan[0])
        upsert_daily_entry(date, plan=args.plan[1].replace("\\n", "\n"))
        return
    if args.actual:
        date = _parse_date(args.actual[0])
        upsert_daily_entry(date, actual=args.actual[1].replace("\\n", "\n"))
        return

    # Handle subcommand-style usage
    if args.cmd == "general":
        update_general_context(args.text.replace("\\n", "\n"))
    elif args.cmd == "plan":
        upsert_daily_entry(_parse_date(args.date), plan=args.text.replace("\\n", "\n"))
    elif args.cmd == "actual":
        upsert_daily_entry(_parse_date(args.date), actual=args.text.replace("\\n", "\n"))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
