"""
tests/test_e2e.py — End-to-end integration test for the daily agent pipeline.

All external I/O is replaced with realistic mocks:
  • Anthropic API  → pipeline.summarizer._call_claude
  • Telegram API   → delivery.telegram_send._send_raw
  • Notion writes  → context.update_context.upsert_daily_entry
  • All 5 collectors (typing, AW, Cursor, meetings, context)

Run with:
    python -m pytest tests/test_e2e.py -v
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys
from typing import Generator
from unittest.mock import MagicMock, call, patch

import pytest

# ── project root on path ──────────────────────────────────────────────────────
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixture_day.json"

# ── Canned Claude responses ───────────────────────────────────────────────────

# Stage 1A: cursor pre-summary (plain text, not JSON)
_PRESUMMARY_TEXT = (
    "Worked on designing the two-stage summarizer pipeline for the daily agent. "
    "Focused on Stage 1A (cursor pre-summarizer using haiku) and Stage 1B "
    "(timeline builder merging ActivityWatch events with typing entries). "
    "The session ended with a clear implementation plan ready for coding."
)

# Stage 2: main summary (must be valid JSON matching the output schema)
_SUMMARY_RESULT: dict = {
    "summary": (
        "Spent the day building the daily agent pipeline's summarizer module. "
        "Designed the two-stage approach: Stage 1A pre-summarizes Cursor sessions "
        "using the cheaper haiku model, while Stage 1B merges ActivityWatch events "
        "with typing entries into a chronological timeline. Attended an Architecture "
        "Review meeting that produced key decisions on merge thresholds and token budget."
    ),
    "highlights": [
        "Designed and scoped the two-stage summarizer architecture",
        "Settled on haiku model for Stage 1A cost optimization",
        "Architecture Review: 60-second merge gap threshold agreed",
    ],
    "tomorrow_plan": [
        {
            "id": 1,
            "task": "Implement _render_timeline_section with token budget",
            "priority": "high",
            "context": "Core pipeline blocker — needed before end-to-end test",
        },
        {
            "id": 2,
            "task": "Wire collect_activitywatch.get_events_date into summarizer",
            "priority": "high",
            "context": "Needed to populate raw_events in timeline builder",
        },
        {
            "id": 3,
            "task": "Run full pipeline end-to-end with fixture_day.json",
            "priority": "medium",
            "context": "Validates token counts and JSON parse",
        },
    ],
    "time_breakdown": [
        {"app": "Cursor",        "minutes": 135, "category": "coding"},
        {"app": "Google Chrome", "minutes":  65, "category": "research"},
        {"app": "Zoom",          "minutes":  55, "category": "meetings"},
        {"app": "Notion",        "minutes":  20, "category": "writing"},
    ],
    "blockers": [
        "ActivityWatch raw_events integration not yet tested with real data",
    ],
}

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def fixture_data() -> dict:
    """Load and parse the shared test fixture once per session."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    raw["date"] = datetime.date.fromisoformat(raw["date"])
    return raw


@pytest.fixture
def plans_dir(tmp_path) -> Generator[pathlib.Path, None, None]:
    """Redirect plan_store writes to a temporary directory."""
    d = tmp_path / "plans"
    d.mkdir()
    with patch("pipeline.plan_store._plans_dir", return_value=d):
        yield d


@pytest.fixture
def pending_dir(tmp_path) -> Generator[pathlib.Path, None, None]:
    """Redirect pending-save writes to a temporary directory."""
    d = tmp_path / "pending"
    d.mkdir()
    with patch("pipeline.run_daily._pending_dir", return_value=d):
        yield d


@pytest.fixture
def micro_dir(tmp_path) -> Generator[pathlib.Path, None, None]:
    """Redirect micro-summary JSONL files to a temporary directory."""
    d = tmp_path / "micro"
    d.mkdir()
    with patch("pipeline.micro_summarizer._micro_dir", return_value=d):
        yield d


@pytest.fixture
def collector_mocks(fixture_data: dict) -> Generator[None, None, None]:
    """Patch all 5 IO collectors to return fixture data, no network calls."""
    with (
        patch("collectors.collect_typing.load_date",
              return_value=fixture_data["typing"]),
        patch("collectors.collect_activitywatch.get_date",
              return_value=fixture_data["activitywatch"]),
        patch("collectors.collect_cursor.get_date",
              return_value=fixture_data["cursor_sessions"]),
        patch("collectors.collect_notion_meetings.get_date",
              return_value=fixture_data["meetings"]),
        patch("context.fetch_context.load",
              return_value=fixture_data["context"]),
    ):
        yield


@pytest.fixture
def claude_mock() -> Generator[MagicMock, None, None]:
    """
    Mock _call_claude with two sequenced responses:
      call 1 → Stage 1A cursor pre-summary (plain text)
      call 2 → Stage 2 main summary (JSON string)
    """
    responses = [
        (_PRESUMMARY_TEXT,             120,  48),
        (json.dumps(_SUMMARY_RESULT),  650, 210),
    ]
    with patch("pipeline.summarizer._call_claude", side_effect=responses) as m:
        yield m


@pytest.fixture
def telegram_messages() -> Generator[list[str], None, None]:
    """
    Capture text of every Telegram message sent during the test.
    Yields the accumulating list so assertions can check it after run().
    """
    sent: list[str] = []

    def _fake_send_raw(text: str, *, parse_mode: str = "Markdown") -> dict:
        sent.append(text)
        return {"ok": True, "result": {"message_id": 42}}

    with patch("delivery.telegram_send._send_raw", side_effect=_fake_send_raw):
        yield sent


@pytest.fixture
def notion_write_mock() -> Generator[MagicMock, None, None]:
    """Patch Notion daily-entry writes as no-ops."""
    with patch("context.update_context.upsert_daily_entry") as m:
        yield m


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_pipeline(date: datetime.date) -> int:
    """Import run_daily fresh and call run(). Returns exit code."""
    from pipeline.run_daily import run
    return run(date)


# ── Test A1: happy path ───────────────────────────────────────────────────────

class TestE2EHappyPath:
    """Full pipeline with all mocks active — nothing should fail."""

    def test_exit_code_zero(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """Pipeline must return exit code 0 on success."""
        code = _run_pipeline(fixture_data["date"])
        assert code == 0, f"Expected exit 0, got {code}"

    def test_schema_summary_field(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """Saved plan payload must have a non-empty summary string."""
        _run_pipeline(fixture_data["date"])
        tomorrow = fixture_data["date"] + datetime.timedelta(days=1)
        payload = json.loads((plans_dir / f"{tomorrow}.json").read_text())
        assert isinstance(payload.get("summary"), str)
        assert len(payload["summary"]) > 20

    def test_schema_plan_tasks(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """
        Saved plan must be a list of tasks, each with required fields and
        valid priority values.
        """
        _run_pipeline(fixture_data["date"])
        tomorrow = fixture_data["date"] + datetime.timedelta(days=1)
        payload  = json.loads((plans_dir / f"{tomorrow}.json").read_text())
        plan     = payload.get("plan", [])

        assert isinstance(plan, list), "plan must be a list"
        assert len(plan) >= 1, "plan must have at least one task"

        for task in plan:
            assert "id"       in task, f"task missing 'id': {task}"
            assert "task"     in task, f"task missing 'task': {task}"
            assert "priority" in task, f"task missing 'priority': {task}"
            assert "done"     in task, f"task missing 'done': {task}"
            assert task["priority"] in ("high", "medium", "low"), (
                f"invalid priority: {task['priority']!r}"
            )

    def test_schema_highlights_and_blockers(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """Plan payload must carry highlights and blockers lists."""
        _run_pipeline(fixture_data["date"])
        tomorrow = fixture_data["date"] + datetime.timedelta(days=1)
        payload  = json.loads((plans_dir / f"{tomorrow}.json").read_text())

        assert isinstance(payload.get("highlights"), list)
        assert isinstance(payload.get("blockers"),   list)

    def test_plan_file_written_for_tomorrow(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """Plan file must exist on disk for tomorrow's date."""
        _run_pipeline(fixture_data["date"])
        tomorrow     = fixture_data["date"] + datetime.timedelta(days=1)
        plan_file    = plans_dir / f"{tomorrow}.json"
        assert plan_file.exists(), f"Expected plan file at {plan_file}"

    def test_telegram_message_non_empty(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """At least one non-empty Telegram message must be sent."""
        _run_pipeline(fixture_data["date"])
        assert len(telegram_messages) >= 1, "No Telegram messages were sent"
        for msg in telegram_messages:
            assert len(msg) > 0, "Telegram message is empty string"

    def test_telegram_message_under_4096_chars(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """Every Telegram message part must be ≤ 4096 characters."""
        _run_pipeline(fixture_data["date"])
        for i, msg in enumerate(telegram_messages):
            assert len(msg) <= 4096, (
                f"Telegram part {i+1} is {len(msg)} chars (max 4096)"
            )

    def test_telegram_message_contains_key_sections(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """Telegram output must include the summary header and plan section."""
        _run_pipeline(fixture_data["date"])
        full_text = "\n".join(telegram_messages)
        assert "Daily Summary" in full_text, "Missing 'Daily Summary' header"
        assert "Tomorrow's Plan" in full_text, "Missing plan section"

    def test_claude_called_twice(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """
        _call_claude should be invoked exactly twice:
          - Once for Stage 1A Cursor pre-summary (1 session in fixture)
          - Once for Stage 2 main summary
        """
        _run_pipeline(fixture_data["date"])
        assert claude_mock.call_count == 2, (
            f"Expected 2 Claude calls, got {claude_mock.call_count}"
        )

    def test_notion_writes_attempted(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """
        upsert_daily_entry must be called for today's actual summary.

        Note: tomorrow's plan is now written as Tasks DB rows (via create_task),
        NOT as a Notion page upsert. So only 1 upsert_daily_entry call is
        expected: today's actual.  The Tasks DB write is attempted but fails
        gracefully in tests (DB not accessible), triggering the page fallback
        which also fails gracefully — either way, the actual is still written.
        """
        _run_pipeline(fixture_data["date"])
        # At least one upsert for today's actual summary
        assert notion_write_mock.call_count >= 1, (
            f"Expected at least 1 Notion write (actual), got {notion_write_mock.call_count}"
        )
        call_dates = {c.args[0] for c in notion_write_mock.call_args_list}
        assert fixture_data["date"] in call_dates, (
            "Today's date (actual summary) must be written to Notion"
        )

    def test_pending_dir_empty_on_success(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """No pending files should be written on a clean run."""
        _run_pipeline(fixture_data["date"])
        assert list(pending_dir.glob("*.json")) == [], (
            "Unexpected pending files written on clean run"
        )


# ── Test A2: collector failure isolation ──────────────────────────────────────

class TestCollectorIsolation:
    """A failing collector must not abort the pipeline."""

    def test_typing_collector_failure_continues(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """typing collector raises → pipeline proceeds, Telegram sent, exit 0."""
        with (
            patch("collectors.collect_typing.load_date",
                  side_effect=RuntimeError("typing daemon not running")),
            patch("collectors.collect_activitywatch.get_date",
                  return_value=fixture_data["activitywatch"]),
            patch("collectors.collect_cursor.get_date",
                  return_value=fixture_data["cursor_sessions"]),
            patch("collectors.collect_notion_meetings.get_date",
                  return_value=fixture_data["meetings"]),
            patch("context.fetch_context.load",
                  return_value=fixture_data["context"]),
        ):
            code = _run_pipeline(fixture_data["date"])

        assert code == 0, f"Pipeline should continue despite typing failure, got {code}"
        assert len(telegram_messages) >= 1, "Telegram should still be sent"

    def test_activitywatch_failure_continues(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        claude_mock, telegram_messages, notion_write_mock,
    ) -> None:
        """AW collector raises → pipeline proceeds, exit 0."""
        with (
            patch("collectors.collect_typing.load_date",
                  return_value=fixture_data["typing"]),
            patch("collectors.collect_activitywatch.get_date",
                  side_effect=ConnectionError("ActivityWatch not running")),
            patch("collectors.collect_cursor.get_date",
                  return_value=fixture_data["cursor_sessions"]),
            patch("collectors.collect_notion_meetings.get_date",
                  return_value=fixture_data["meetings"]),
            patch("context.fetch_context.load",
                  return_value=fixture_data["context"]),
        ):
            code = _run_pipeline(fixture_data["date"])

        assert code == 0

    def test_all_collectors_fail_but_summarizer_runs(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        telegram_messages, notion_write_mock,
    ) -> None:
        """
        Even if every collector fails, the pipeline should call the summarizer
        with empty defaults and (assuming Claude returns valid JSON) succeed.

        When all collectors fail, cursor_sessions=[] so Stage 1A makes 0 calls.
        Only Stage 2 runs → exactly 1 Claude call needed.
        """
        # Only one Claude call when 0 cursor sessions (Stage 1A is skipped)
        one_shot = [(json.dumps(_SUMMARY_RESULT), 650, 210)]

        with (
            patch("pipeline.summarizer._call_claude", side_effect=one_shot),
            patch("collectors.collect_typing.load_date",
                  side_effect=Exception("fail")),
            patch("collectors.collect_activitywatch.get_date",
                  side_effect=Exception("fail")),
            patch("collectors.collect_cursor.get_date",
                  side_effect=Exception("fail")),
            patch("collectors.collect_notion_meetings.get_date",
                  side_effect=Exception("fail")),
            patch("context.fetch_context.load",
                  side_effect=Exception("fail")),
        ):
            code = _run_pipeline(fixture_data["date"])

        # Summarizer still runs with empty inputs; Claude still returns valid JSON
        assert code == 0
        assert len(telegram_messages) >= 1, "Telegram should still be sent"


# ── Test A3: summarizer error handling ───────────────────────────────────────

class TestSummarizerErrors:
    """Verify correct behavior when Claude returns bad output."""

    def test_bad_json_saves_pending_and_exits_1(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, notion_write_mock,
    ) -> None:
        """
        If _call_claude returns un-parseable JSON for Stage 2, the pipeline
        must: save raw data to pending/, send an error notification, exit 1.
        """
        # Stage 1A returns valid text; Stage 2 returns garbage
        bad_responses = [
            (_PRESUMMARY_TEXT, 120, 48),
            ("Sorry, I cannot help with that today.", 100, 20),  # not JSON
        ]
        error_sent: list[str] = []

        def _fake_send_error(error):
            error_sent.append(str(error))

        with (
            patch("pipeline.summarizer._call_claude", side_effect=bad_responses),
            patch("delivery.telegram_send._send_raw",
                  return_value={"ok": True, "result": {}}),
            patch("delivery.telegram_send.send_error", side_effect=_fake_send_error),
        ):
            code = _run_pipeline(fixture_data["date"])

        assert code == 1, f"Expected exit 1 on JSON parse failure, got {code}"

        # Pending file should have been saved
        pending_files = list(pending_dir.glob("*.json"))
        assert len(pending_files) == 1, (
            f"Expected 1 pending file, got {pending_files}"
        )
        pending_data = json.loads(pending_files[0].read_text())
        assert "error" in pending_data, "Pending file must record the error"

    def test_plan_not_written_on_summarizer_failure(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, notion_write_mock,
    ) -> None:
        """No plan file should exist when the summarizer fails hard."""
        bad_responses = [
            (_PRESUMMARY_TEXT, 120, 48),
            ("this is not json", 100, 20),
        ]
        with (
            patch("pipeline.summarizer._call_claude", side_effect=bad_responses),
            patch("delivery.telegram_send._send_raw",
                  return_value={"ok": True, "result": {}}),
            patch("delivery.telegram_send.send_error", return_value=None),
        ):
            _run_pipeline(fixture_data["date"])

        assert list(plans_dir.glob("*.json")) == [], (
            "No plan file should be written when the summarizer fails"
        )


# ── Test A4: dry-run mode ─────────────────────────────────────────────────────

class TestDryRun:
    """--dry-run flag: summarize but do NOT save plan or send Telegram."""

    def test_dry_run_does_not_send_telegram(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, notion_write_mock,
    ) -> None:
        """In dry-run mode, _send_raw must never be called."""
        send_raw_mock = MagicMock(return_value={"ok": True, "result": {}})
        with patch("delivery.telegram_send._send_raw", send_raw_mock):
            from pipeline.run_daily import run
            code = run(fixture_data["date"], dry_run=True)
        assert code == 0
        send_raw_mock.assert_not_called()

    def test_dry_run_does_not_write_plan_file(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock, notion_write_mock,
    ) -> None:
        """In dry-run mode, no plan file should be written."""
        with patch("delivery.telegram_send._send_raw",
                   return_value={"ok": True, "result": {}}):
            from pipeline.run_daily import run
            run(fixture_data["date"], dry_run=True)
        assert list(plans_dir.glob("*.json")) == [], (
            "plan file must not be written in dry-run"
        )

    def test_dry_run_does_not_write_notion(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, claude_mock,
    ) -> None:
        """In dry-run mode, Notion writes must be skipped."""
        notion_mock = MagicMock()
        with (
            patch("context.update_context.upsert_daily_entry", notion_mock),
            patch("delivery.telegram_send._send_raw",
                  return_value={"ok": True, "result": {}}),
        ):
            from pipeline.run_daily import run
            run(fixture_data["date"], dry_run=True)
        notion_mock.assert_not_called()


# ── Test B: micro-summarizer ──────────────────────────────────────────────────

class TestMicroSummarizer:
    """Unit tests for micro_summarizer.py."""

    def test_get_micro_summaries_returns_empty_when_no_file(
        self, micro_dir,
    ) -> None:
        """No JSONL file → empty list, no exception."""
        from pipeline.micro_summarizer import get_micro_summaries
        result = get_micro_summaries(datetime.date(2026, 5, 29))
        assert result == []

    def test_get_micro_summaries_reads_jsonl(
        self, micro_dir,
    ) -> None:
        """Written JSONL entries are read back correctly."""
        from pipeline.micro_summarizer import get_micro_summaries

        entries = [
            {
                "timestamp":    "2026-05-29T10:30:00+00:00",
                "window_start": "2026-05-29T10:00:00+00:00",
                "window_end":   "2026-05-29T10:30:00+00:00",
                "app":          "Cursor",
                "minutes":      25.0,
                "summary":      "Implemented the timeline builder merge logic.",
                "model":        "claude-haiku-4-5-20251001",
                "tokens":       68,
            },
            {
                "timestamp":    "2026-05-29T11:00:00+00:00",
                "window_start": "2026-05-29T10:30:00+00:00",
                "window_end":   "2026-05-29T11:00:00+00:00",
                "app":          "Chrome",
                "minutes":      18.0,
                "summary":      "Researched ActivityWatch API documentation.",
                "model":        "claude-haiku-4-5-20251001",
                "tokens":       52,
            },
        ]
        d = datetime.date(2026, 5, 29)
        path = micro_dir / f"{d}.jsonl"
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        result = get_micro_summaries(d)
        assert len(result) == 2
        assert result[0]["app"]     == "Cursor"
        assert result[1]["app"]     == "Chrome"
        assert result[0]["summary"] == "Implemented the timeline builder merge logic."

    def test_micro_summaries_used_in_prompt(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, notion_write_mock,
    ) -> None:
        """
        When micro-summaries exist, build_prompt must produce a prompt
        that contains the 'ACTIVITY SUMMARIES' section instead of
        'YOUR DAY — CHRONOLOGICAL'.
        """
        from pipeline.micro_summarizer import get_micro_summaries
        import pipeline.summarizer as sm

        # Populate micro JSONL for fixture date
        micro_entries = [
            {
                "timestamp":    "2026-05-29T11:00:00+00:00",
                "window_start": "2026-05-29T10:30:00+00:00",
                "window_end":   "2026-05-29T11:00:00+00:00",
                "app":          "Cursor",
                "minutes":      28.0,
                "summary":      "Implemented the timeline builder in summarizer.py.",
                "model":        "claude-haiku-4-5-20251001",
                "tokens":       71,
            },
        ]
        d = fixture_data["date"]
        micro_file = micro_dir / f"{d}.jsonl"
        with micro_file.open("w") as f:
            for e in micro_entries:
                f.write(json.dumps(e) + "\n")

        # Build a timeline and prompt
        timeline = sm.build_timeline(
            fixture_data["typing"],
            fixture_data["activitywatch"],
            [],
        )
        # With micro-summaries
        prompt_with_micro = sm.build_prompt(
            timeline,
            fixture_data["meetings"],
            fixture_data["context"],
            d,
            micro_summaries=micro_entries,
        )
        # Without micro-summaries
        prompt_without = sm.build_prompt(
            timeline,
            fixture_data["meetings"],
            fixture_data["context"],
            d,
            micro_summaries=None,
        )

        assert "ACTIVITY SUMMARIES" in prompt_with_micro, (
            "Micro path must include 'ACTIVITY SUMMARIES' section"
        )
        assert "YOUR DAY — CHRONOLOGICAL" not in prompt_with_micro, (
            "Micro path must NOT include 'CHRONOLOGICAL' section"
        )
        assert "YOUR DAY — CHRONOLOGICAL" in prompt_without, (
            "Normal path must include 'CHRONOLOGICAL' section"
        )
        assert len(prompt_with_micro) < len(prompt_without), (
            "Micro prompt should be shorter (token reduction)"
        )

    def test_run_micro_summary_writes_jsonl(
        self, micro_dir,
    ) -> None:
        """run_micro_summary appends valid JSON lines to the JSONL file."""
        from pipeline.micro_summarizer import run_micro_summary, get_micro_summaries

        d   = datetime.date(2026, 5, 29)
        now = datetime.datetime(2026, 5, 29, 11, 0, 0, tzinfo=datetime.timezone.utc)

        fake_typing = [
            {
                "timestamp": "2026-05-29T10:45:00+00:00",
                "app": "Cursor",
                "bundle_id": "com.todesktop.230313mzl4w4u92",
                "window_title": "summarizer.py — daily_agent",
                "text": "def build_timeline(typing_entries, activitywatch):",
                "_mode": "full",
            },
        ]
        fake_events = [
            {
                "app": "Cursor",
                "title": "summarizer.py — daily_agent",
                "domain": None,
                "start_iso": "2026-05-29T10:30:00+00:00",
                "end_iso":   "2026-05-29T11:00:00+00:00",
                "duration_seconds": 1800,
            },
        ]

        def _fake_claude_call(*args, **kwargs):
            """Return a mock message object as anthropic.Anthropic.messages.create would."""
            content_mock = MagicMock()
            content_mock.text = "Implemented the timeline builder's segment merge logic."
            usage_mock = MagicMock()
            usage_mock.input_tokens  = 80
            usage_mock.output_tokens = 18
            result_mock = MagicMock()
            result_mock.content = [content_mock]
            result_mock.usage   = usage_mock
            return result_mock

        with (
            patch("collectors.collect_typing.load_date", return_value=fake_typing),
            patch("collectors.collect_activitywatch.get_events_date",
                  return_value=fake_events),
            patch("anthropic.Anthropic") as mock_anthropic_cls,
        ):
            mock_anthropic_cls.return_value.messages.create.side_effect = _fake_claude_call
            summaries = run_micro_summary(window_minutes=30, date=d, now=now)

        assert len(summaries) == 1, f"Expected 1 summary, got {len(summaries)}"
        assert summaries[0]["app"]     == "Cursor"
        assert "timeline" in summaries[0]["summary"].lower() or \
               "merge"    in summaries[0]["summary"].lower() or \
               len(summaries[0]["summary"]) > 10

        # File should exist on disk
        path = micro_dir / f"{d}.jsonl"
        assert path.exists(), f"JSONL file not written: {path}"
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["app"]     == "Cursor"
        assert parsed["minutes"] > 0
        assert isinstance(parsed["tokens"], int)

    def test_run_micro_summary_dry_run_no_file(
        self, micro_dir,
    ) -> None:
        """dry_run=True must NOT write any file."""
        from pipeline.micro_summarizer import run_micro_summary

        d   = datetime.date(2026, 5, 29)
        now = datetime.datetime(2026, 5, 29, 11, 0, 0, tzinfo=datetime.timezone.utc)

        fake_typing = [{
            "timestamp": "2026-05-29T10:45:00+00:00",
            "app": "Cursor",
            "bundle_id": "com.todesktop.230313mzl4w4u92",
            "text": "some code",
            "_mode": "full",
        }]
        fake_events = [{
            "app": "Cursor",
            "start_iso": "2026-05-29T10:30:00+00:00",
            "end_iso":   "2026-05-29T11:00:00+00:00",
            "duration_seconds": 1800,
        }]

        content_mock = MagicMock()
        content_mock.text = "Wrote code for the summarizer."
        usage_mock = MagicMock()
        usage_mock.input_tokens  = 60
        usage_mock.output_tokens = 12

        with (
            patch("collectors.collect_typing.load_date", return_value=fake_typing),
            patch("collectors.collect_activitywatch.get_events_date",
                  return_value=fake_events),
            patch("anthropic.Anthropic") as mock_anthropic_cls,
        ):
            msg_mock = MagicMock()
            msg_mock.content = [content_mock]
            msg_mock.usage   = usage_mock
            mock_anthropic_cls.return_value.messages.create.return_value = msg_mock

            result = run_micro_summary(
                window_minutes=30, date=d, now=now, dry_run=True
            )

        assert len(result) >= 1, "Should compute summaries even in dry_run"
        assert not (micro_dir / f"{d}.jsonl").exists(), (
            "dry_run=True must not write any file"
        )

    def test_e2e_pipeline_with_micro_summaries(
        self,
        fixture_data, plans_dir, pending_dir, micro_dir,
        collector_mocks, notion_write_mock,
    ) -> None:
        """
        Full pipeline run with pre-existing micro-summaries:
        should still succeed (exit 0) and send a Telegram message.
        """
        # Write micro-summaries for fixture date
        micro_entries = [
            {
                "timestamp":    "2026-05-29T10:30:00+00:00",
                "window_start": "2026-05-29T10:00:00+00:00",
                "window_end":   "2026-05-29T10:30:00+00:00",
                "app":          "Cursor",
                "minutes":      27.0,
                "summary":      "Built Stage 1A cursor pre-summarizer using haiku model.",
                "model":        "claude-haiku-4-5-20251001",
                "tokens":       75,
            },
        ]
        d = fixture_data["date"]
        with (micro_dir / f"{d}.jsonl").open("w") as f:
            for e in micro_entries:
                f.write(json.dumps(e) + "\n")

        # Stage 1A still runs (1 cursor session), Stage 2 returns valid JSON
        # BUT the prompt now uses the micro-summaries section
        prompt_seen: list[str] = []

        def _capture_claude(system, user, max_tokens, model, temperature=0, call_type=""):
            prompt_seen.append(user)
            if len(prompt_seen) == 1:
                return (_PRESUMMARY_TEXT, 120, 48)
            return (json.dumps(_SUMMARY_RESULT), 650, 210)

        sent: list[str] = []

        def _fake_send_raw(text, *, parse_mode="Markdown"):
            sent.append(text)
            return {"ok": True, "result": {"message_id": 42}}

        with (
            patch("pipeline.summarizer._call_claude", side_effect=_capture_claude),
            patch("delivery.telegram_send._send_raw", side_effect=_fake_send_raw),
        ):
            from pipeline.run_daily import run
            code = run(d)

        assert code == 0
        assert len(sent) >= 1

        # The Stage-2 prompt (index 1) must use micro-summaries section
        assert len(prompt_seen) == 2, f"Expected 2 Claude calls, got {len(prompt_seen)}"
        stage2_prompt = prompt_seen[1]
        assert "ACTIVITY SUMMARIES" in stage2_prompt, (
            "Stage-2 prompt must use micro-summaries section when JSONL exists"
        )
