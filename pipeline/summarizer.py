"""
Daily summarizer — two-stage pipeline.

Stage 1A  presummary_cursor_session / presummary_all_cursor
          Cheap haiku call per Cursor session → 3-5 sentence condensed summary.

Stage 1B  build_timeline
          Merges AW raw events + typing entries + cursor pre-summaries into a
          single sorted, budget-constrained chronological timeline.

Stage 2   summarize
          Builds the final prompt from timeline + meetings + context,
          calls the main Claude model, parses + validates JSON.

Also exposes parse_plan_edit for the Telegram plan-edit webhook.

CLI
---
  python pipeline/summarizer.py --test               # full end-to-end from fixture
  python pipeline/summarizer.py --test --stage1-only # Stage 1A+1B only (no API call)
  python pipeline/summarizer.py --test --show-prompt # print final prompt and exit
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys
import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.daily_trace import DailyPipelineTrace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import anthropic

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("summarizer")

# ── bundle_id → AW app-name mapping (for typing attachment) ──────────────────

_BUNDLE_TO_AW_APP: dict[str, str] = {
    "com.google.Chrome":               "Google Chrome",
    "com.apple.Safari":                "Safari",
    "org.mozilla.firefox":             "Firefox",
    "company.thebrowser.Browser":      "Arc",
    "com.todesktop.230313mzl4w4u92":   "Cursor",
    "com.microsoft.VSCode":            "Code",
    "io.claude.app":                   "Claude",
    "com.anthropic.claudefordesktop":  "Claude",
    "com.notion.mac":                  "Notion",
    "com.apple.Notes":                 "Notes",
    "com.microsoft.Word":              "Microsoft Word",
    "com.microsoft.Powerpoint":        "Microsoft PowerPoint",
    "com.tinyspeck.slackmacgap":       "Slack",
    "com.apple.mail":                  "Mail",
    "com.apple.MobileSMS":             "Messages",
    "zoom.us":                         "Zoom",
    "com.googlecode.iterm2":           "iTerm2",
    "com.apple.Terminal":              "Terminal",
}


def _app_base_name(app_name: str) -> str:
    """Strip ActivityWatch domain suffix: 'Google Chrome/claude.ai' → 'Google Chrome'."""
    return app_name.split("/", 1)[0].strip()


def _bundle_matches_app(bundle_id: str, app_name: str) -> bool:
    """
    Return True if *bundle_id* is consistent with *app_name*.
    Unknown bundle_ids use a loose substring match; missing bundle_id always passes.
    """
    base = _app_base_name(app_name)
    if not bundle_id:
        return True
    mapped = _BUNDLE_TO_AW_APP.get(bundle_id)
    if mapped is not None:
        return mapped.lower() == base.lower()
    # Unknown bundle: check if any meaningful part of the bundle appears in app name
    app_lower = base.lower()
    for part in bundle_id.lower().split("."):
        if len(part) > 3 and part in app_lower:
            return True
    return False


def _entry_matches_segment(entry: dict, seg_app: str) -> bool:
    """Match typing entry to an AW segment by bundle_id and/or display app name."""
    if _bundle_matches_app(entry.get("bundle_id", ""), seg_app):
        return True
    entry_app = (entry.get("app") or "").strip()
    if entry_app and entry_app.lower() == _app_base_name(seg_app).lower():
        return True
    return False


# ── low-level helpers ─────────────────────────────────────────────────────────

def _parse_iso(iso_str: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _epoch(iso_str: str) -> float:
    """ISO timestamp → Unix epoch seconds."""
    return _parse_iso(iso_str).timestamp()


def _local_hhmm(iso_str: str) -> str:
    """ISO timestamp → local HH:MM string (uses configured timezone)."""
    if not iso_str:
        return "??:??"
    try:
        dt = _parse_iso(iso_str)
        import pytz
        cfg = get_config()
        tz = pytz.timezone(cfg.get("timezone", "Asia/Jerusalem"))
        return dt.astimezone(tz).strftime("%H:%M")
    except Exception:
        try:
            return _parse_iso(iso_str).strftime("%H:%M")
        except Exception:
            return "??:??"


# ── Claude API helper ─────────────────────────────────────────────────────────

def _call_claude(
    system: str,
    user: str,
    max_tokens: int,
    model: str,
    temperature: float = 0,
    call_type: str = "",
) -> tuple[str, int, int]:
    """
    Call the Anthropic Messages API.

    Returns (response_text, input_tokens, output_tokens).
    Raises anthropic.APIError on failure.

    Parameters
    ----------
    call_type : Human-readable label logged to the cost JSONL file.
                e.g. "synthesis", "cursor_presummary", "compression", "plan_edit".
    """
    cfg = get_config()
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    in_tok  = msg.usage.input_tokens
    out_tok = msg.usage.output_tokens

    # ── Cost logging (never raises) ───────────────────────────────────────────
    try:
        from utils.cost_logger import log_api_call
        log_api_call(model, in_tok, out_tok, call_type)
    except Exception:
        pass

    return msg.content[0].text, in_tok, out_tok


def _extract_json(text: str) -> dict | list:
    """
    Extract JSON from a Claude response.
    Handles bare JSON and ```json...``` / ```...``` code fences.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end]).strip()
    return json.loads(text)


# ── Per-source size guard ─────────────────────────────────────────────────────

# Default per-source token limit.  Overridden by config["max_tokens_per_source"].
# Any single source blob that exceeds this gets a cheap compression pass before
# it reaches the main synthesis prompt.
MAX_TOKENS_PER_SOURCE: int = 8000

_COMPRESS_SYSTEM = (
    "You are a content condenser. Given a passage of text, output a compressed "
    "version that retains all key facts, decisions, outcomes, tasks, and blockers. "
    "Remove filler words, repetition, pleasantries, and verbosity. "
    "Plain text only — no bullet points, no headers, no metadata."
)


def _estimate_tokens(text: str) -> int:
    """Rough token count: 4 chars ≈ 1 token (works well for English/code)."""
    return max(1, len(text) // 4)


# Per-entry line cap applied to every individual raw data item before it reaches
# the prompt.  20 lines ≈ 80–120 tokens — enough to capture the gist of any
# single typed message or Cursor turn without letting one long entry dominate.
_MAX_ENTRY_LINES: int = 20


def _truncate_entry(text: str, max_lines: int = _MAX_ENTRY_LINES) -> str:
    """Hard-truncate *text* to *max_lines* lines, appending a count if cut."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[:max_lines]
    omitted = len(lines) - max_lines
    kept.append(f"[... {omitted} more line(s) truncated]")
    return "\n".join(kept)


def _maybe_compress(
    text: str,
    source_name: str,
    max_tokens: int,
    model: str,
) -> str:
    """
    Compress *text* with a cheap model call when it exceeds *max_tokens*.

    Behaviour
    ---------
    • If ``_estimate_tokens(text) <= max_tokens`` → returns *text* unchanged
      (no API call, no log noise).
    • Otherwise → calls *model* (typically haiku) and targets roughly half the
      limit in the compressed output.
    • Logs a WARNING when compression fires, with source name, before/after
      token counts, and reduction percentage.
    • On API failure → falls back to hard truncation at the limit and logs a
      WARNING.

    Parameters
    ----------
    text        : Raw source text to check/compress.
    source_name : Human-readable label used in log lines (e.g. ``"meetings"``).
    max_tokens  : Token budget.  Comes from ``config["max_tokens_per_source"]``
                  or the module-level ``MAX_TOKENS_PER_SOURCE`` constant.
    model       : Cheap/fast model to use (haiku-class).
    """
    if not text:
        return text

    approx_tokens = _estimate_tokens(text)
    if approx_tokens <= max_tokens:
        return text

    target_tokens = max(max_tokens // 2, 300)  # aim for ~half, floor at 300
    log.warning(
        "Source '%s' oversized: ~%d tokens (limit=%d) — "
        "compressing to ~%d tokens using %s",
        source_name, approx_tokens, max_tokens, target_tokens, model,
    )

    user_msg = (
        f"Compress the following text to approximately {target_tokens} tokens "
        f"({target_tokens * 4} characters). "
        f"Preserve all key information. Remove filler and repetition.\n\n"
        f"{text}"
    )

    try:
        compressed, in_tok, out_tok = _call_claude(
            _COMPRESS_SYSTEM,
            user_msg,
            max_tokens=target_tokens + 100,   # small headroom above target
            model=model,
            temperature=0,
            call_type="compression",
        )
        compressed = compressed.strip()
        compressed_tokens = _estimate_tokens(compressed)
        reduction_pct = 100.0 * (1.0 - compressed_tokens / approx_tokens)
        log.info(
            "Compression done  source='%s'  %d→%d tokens  "
            "%.0f%% reduction  [API: %d in + %d out]",
            source_name, approx_tokens, compressed_tokens,
            reduction_pct, in_tok, out_tok,
        )
        return compressed
    except Exception as exc:
        # Hard-truncate as a safe fallback so the pipeline never blocks
        log.warning(
            "Compression of '%s' failed (%s) — "
            "hard truncating to %d tokens instead",
            source_name, exc, max_tokens,
        )
        cutoff = max_tokens * 4
        return text[:cutoff] + f"\n[truncated — was ~{approx_tokens} tokens]"


# ── Second-pass compression ──────────────────────────────────────────────────

# Default total-prompt token threshold that triggers second-pass compression
# when the --compress flag is active.  Override via config["max_synthesis_tokens"].
MAX_SYNTHESIS_TOKENS: int = 12_000

_COMPRESS_TO_BULLETS_SYSTEM = (
    "You compress text into the most information-dense bullet points possible. "
    "Output ONLY bullet points, one per line, each starting with '• '. "
    "No headers, no titles, no preamble, no commentary after the last bullet."
)


def _compress_to_bullets(
    text: str,
    source_name: str,
    max_bullets: int,
    model: str,
) -> str:
    """
    Compress *text* into at most *max_bullets* bullet points using *model*.

    Each bullet is one crisp sentence. Returns the original text unchanged if
    compression is skipped (text too short) or fails (API error).

    Logs at INFO when applied, at WARNING when it falls back to the original.
    """
    if not text or len(text.strip()) < 100:   # nothing worth compressing
        return text

    before_tokens = _estimate_tokens(text)

    user_msg = (
        f"Compress the following {source_name} into at most {max_bullets} "
        f"bullet points. Each bullet must be one specific, concrete sentence. "
        f"Keep the most important facts, decisions, and outcomes. "
        f"Discard filler, repetition, and low-value details.\n\n"
        f"{text}"
    )

    try:
        compressed, in_tok, out_tok = _call_claude(
            _COMPRESS_TO_BULLETS_SYSTEM,
            user_msg,
            max_tokens=max_bullets * 80,   # ~80 tokens per bullet
            model=model,
            temperature=0,
            call_type="second_pass_compression",
        )
        compressed = compressed.strip()
        after_tokens = _estimate_tokens(compressed)
        reduction = 100.0 * (1.0 - after_tokens / max(before_tokens, 1))
        log.info(
            "Second-pass  source='%s'  %d→%d tokens  %.0f%% reduction",
            source_name, before_tokens, after_tokens, reduction,
        )
        return compressed
    except Exception as exc:
        log.warning(
            "Second-pass compression failed for '%s' (%s) — using original",
            source_name, exc,
        )
        return text


# ── Stage 1A: Cursor pre-summarizer ──────────────────────────────────────────

_CURSOR_PRESUMMARY_SYSTEM = (
    "You summarize coding sessions. Extract only what matters for a daily "
    "productivity review. Be specific about what was built or fixed. "
    "Ignore: code listings, error messages, repeated attempts, file contents "
    "pasted as context."
)


def _format_turns_for_presummary(user_turns: list[str], max_chars: int = 3000) -> str:
    """Number the turns; truncate with a count message if over max_chars."""
    lines: list[str] = []
    total = 0
    for i, turn in enumerate(user_turns, 1):
        line = f"{i}. {_truncate_entry(turn.strip())}\n"
        if total + len(line) > max_chars:
            remaining = len(user_turns) - i + 1
            lines.append(f"[... {remaining} more turn(s) truncated]\n")
            break
        lines.append(line)
        total += len(line)
    return "".join(lines).rstrip()


def presummary_cursor_session(session: dict) -> dict:
    """
    Run a cheap haiku call to condense one Cursor session into 3-5 sentences.

    Input  – one session dict from collect_cursor:
               {session_id, workspace, started_at, user_turns, turn_count}
    Output – {session_id, started_at, workspace, summary, token_count}
    Falls back to the main model if the haiku call fails.
    """
    cfg = get_config()
    haiku_model: str = cfg.get(
        "anthropic_cursor_presummary_model", "claude-haiku-4-5-20251001"
    )
    main_model: str = cfg.get("anthropic_model", "claude-sonnet-4-6")

    session_id = session.get("session_id", "?")
    workspace = session.get("workspace", "unknown")
    started_at = session.get("started_at", "")
    user_turns = session.get("user_turns", [])
    turn_count = session.get("turn_count", len(user_turns))

    turns_text = _format_turns_for_presummary(user_turns)

    user_msg = (
        f"Workspace: {workspace}\n"
        f"Session started: {started_at}\n"
        f"User turns ({turn_count} total):\n"
        f"{turns_text}\n\n"
        "Summarize:\n"
        "- What problem or task was being worked on (one sentence)\n"
        "- The approach taken\n"
        "- Whether it was resolved or left open\n"
        "- Any key decisions, insights, or blockers\n"
        "Output: 3-5 sentences, plain text, no bullet points."
    )

    token_count = 0
    for model in (haiku_model, main_model):
        try:
            text, in_tok, out_tok = _call_claude(
                _CURSOR_PRESUMMARY_SYSTEM, user_msg,
                max_tokens=200, model=model, temperature=0,
                call_type="cursor_presummary",
            )
            token_count = in_tok + out_tok
            log.info(
                "Cursor pre-summary %s: model=%s tokens=%d+%d",
                session_id[:16], model, in_tok, out_tok,
            )
            return {
                "session_id": session_id,
                "started_at": started_at,
                "workspace": workspace,
                "summary": text.strip(),
                "token_count": token_count,
            }
        except anthropic.APIError as exc:
            if model == haiku_model:
                log.warning(
                    "Haiku pre-summary failed for %s (%s) — retrying with %s",
                    session_id[:16], exc, main_model,
                )
            else:
                log.error("Pre-summary failed for %s: %s", session_id[:16], exc)
                break
        except Exception as exc:
            log.error("Unexpected pre-summary error for %s: %s", session_id[:16], exc)
            break

    return {
        "session_id": session_id,
        "started_at": started_at,
        "workspace": workspace,
        "summary": "[summarization failed]",
        "token_count": 0,
    }


def presummary_all_cursor(sessions: list[dict]) -> list[dict]:
    """
    Run presummary_cursor_session() for each session sequentially.
    Failures are logged and replaced with a placeholder summary.
    Returns total Stage-1A token count as the last element's sentinel
    (callers sum token_count from each entry).
    """
    results: list[dict] = []
    for session in sessions:
        try:
            result = presummary_cursor_session(session)
        except Exception as exc:
            sid = session.get("session_id", "?")
            log.warning("presummary_cursor_session raised for %s: %s", sid, exc)
            result = {
                "session_id": session.get("session_id", "?"),
                "started_at": session.get("started_at", ""),
                "workspace": session.get("workspace", "unknown"),
                "summary": "[summarization failed]",
                "token_count": 0,
            }
        results.append(result)

    total_tokens = sum(r.get("token_count", 0) for r in results)
    log.info("Stage 1A complete: %d sessions, %d total tokens", len(results), total_tokens)
    return results


# ── Stage 1B: Timeline builder ────────────────────────────────────────────────

def _merge_into_segments(raw_events: list[dict], min_secs: float) -> list[dict]:
    """
    Merge consecutive same-app events with gap < 60 s, then drop segments
    shorter than *min_secs*.

    Returns list of internal segment dicts:
      {app, title, domain, start_iso, end_iso, duration_seconds, _raw_count}
    """
    if not raw_events:
        return []

    sorted_evs = sorted(raw_events, key=lambda e: e["start_iso"])
    segments: list[dict] = []

    for ev in sorted_evs:
        if not segments or segments[-1]["app"] != ev["app"]:
            segments.append({
                "app": ev["app"],
                "title": ev["title"],
                "domain": ev.get("domain"),
                "start_iso": ev["start_iso"],
                "end_iso": ev["end_iso"],
                "duration_seconds": ev["duration_seconds"],
                "_raw_count": 1,
            })
            continue

        last = segments[-1]
        gap = _epoch(ev["start_iso"]) - _epoch(last["end_iso"])
        if gap < 60:
            # Absorb: extend end, accumulate duration (including the gap itself)
            last["end_iso"] = ev["end_iso"]
            last["duration_seconds"] += ev["duration_seconds"] + max(0.0, gap)
            last["_raw_count"] += 1
            # Promote domain if first event had none
            if ev.get("domain") and not last["domain"]:
                last["domain"] = ev["domain"]
        else:
            segments.append({
                "app": ev["app"],
                "title": ev["title"],
                "domain": ev.get("domain"),
                "start_iso": ev["start_iso"],
                "end_iso": ev["end_iso"],
                "duration_seconds": ev["duration_seconds"],
                "_raw_count": 1,
            })

    kept = [s for s in segments if s["duration_seconds"] >= min_secs]
    dropped = len(segments) - len(kept)
    if dropped:
        log.debug("Timeline: dropped %d sub-%ds segments", dropped, int(min_secs))
    return kept


def _attach_typing_to_segments(
    segments: list[dict],
    typing_entries: list[dict],
) -> None:
    """
    Mutate segments in place: add '_typing_entries' (full entry dicts)
    and 'typing' (list of text strings for full/domain-mode entries).

    Matching rule: entry.timestamp in [seg.start-30s, seg.end+30s]
                   AND entry.bundle_id consistent with seg.app.
    """
    for seg in segments:
        try:
            seg_s = _epoch(seg["start_iso"]) - 30.0
            seg_e = _epoch(seg["end_iso"]) + 30.0
        except Exception:
            seg["_typing_entries"] = []
            seg["typing"] = []
            continue

        matched: list[dict] = []
        for entry in typing_entries:
            ts_str = entry.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = _epoch(ts_str)
            except Exception:
                continue
            if not (seg_s <= ts <= seg_e):
                continue
            if not _entry_matches_segment(entry, seg["app"]):
                continue
            matched.append(entry)

        cfg = get_config()
        max_per_seg = int(cfg.get("max_typing_per_segment", 6))
        seg["_typing_entries"] = matched
        seg["typing"] = [
            _truncate_entry(e.get("text", "").strip())
            for e in matched[:max_per_seg]
            if e.get("text", "").strip()
        ]
        if len(matched) > max_per_seg:
            seg["_typing_truncated"] = len(matched) - max_per_seg


def _attach_cursor_presummaries(
    segments: list[dict],
    cursor_presummaries: list[dict],
) -> list[dict]:
    """
    Attach cursor presummaries to matching segments (by timestamp overlap).
    Creates a synthetic segment if no matching segment exists.

    Returns the (possibly extended) segments list.
    """
    for pre in cursor_presummaries:
        started_at = pre.get("started_at", "")
        if not started_at:
            continue
        try:
            start_ep = _epoch(started_at)
        except Exception:
            continue

        matched_seg = None
        for seg in segments:
            try:
                if _epoch(seg["start_iso"]) <= start_ep <= _epoch(seg["end_iso"]):
                    matched_seg = seg
                    break
            except Exception:
                continue

        if matched_seg is not None:
            matched_seg["cursor_summary"] = pre.get("summary", "")
        else:
            log.debug(
                "No AW segment for Cursor session %s — creating synthetic",
                pre.get("session_id", "?")[:16],
            )
            segments.append({
                "app": "Cursor",
                "title": f"workspace: {pre.get('workspace', 'unknown')}",
                "domain": None,
                "start_iso": started_at,
                "end_iso": started_at,
                "duration_seconds": 0,
                "_raw_count": 0,
                "_typing_entries": [],
                "typing": [],
                "cursor_summary": pre.get("summary", ""),
                "synthetic": True,
            })

    return segments


def _determine_capture_mode(seg: dict) -> str:
    if seg.get("cursor_summary"):
        return "full"
    modes = [e.get("_mode", "charcount") for e in seg.get("_typing_entries", [])]
    if any(m in ("full", "domain") for m in modes):
        return "full"
    if "summary" in modes:
        return "summary"
    return "charcount"


def build_timeline(
    typing_entries: list[dict],
    activitywatch: dict,
    cursor_presummaries: list[dict],
) -> list[dict]:
    """
    Merge AW raw events, typing entries, and cursor pre-summaries into a
    single chronological timeline.

    Returns list of timeline entry dicts:
      { start, end, duration_minutes, app, domain, typing, cursor_summary,
        capture_mode, synthetic }
    """
    cfg = get_config()
    min_secs = cfg.get("min_segment_minutes", 3) * 60

    raw_events: list[dict] = activitywatch.get("raw_events", [])
    if not raw_events:
        log.warning(
            "build_timeline: activitywatch has no raw_events — "
            "only synthetic Cursor segments will appear"
        )

    # Step 1: merge into segments
    segments = _merge_into_segments(raw_events, min_secs)

    # Step 2: attach typing entries
    _attach_typing_to_segments(segments, typing_entries)

    # Step 3: attach cursor presummaries (may add synthetic segments)
    segments = _attach_cursor_presummaries(segments, cursor_presummaries)

    # Step 4: format output
    timeline: list[dict] = []
    for seg in sorted(segments, key=lambda s: s["start_iso"]):
        domain = seg.get("domain")
        app_label = f"{seg['app']}/{domain}" if domain else seg["app"]

        timeline.append({
            "start":            _local_hhmm(seg["start_iso"]),
            "end":              _local_hhmm(seg["end_iso"]),
            "duration_minutes": max(0, round(seg["duration_seconds"] / 60)),
            "app":              app_label,
            "domain":           domain,
            "typing":           seg.get("typing", []),
            "cursor_summary":   seg.get("cursor_summary"),
            "capture_mode":     _determine_capture_mode(seg),
            "synthetic":        seg.get("synthetic", False),
        })

    log.info("build_timeline: %d segments from %d raw events", len(timeline), len(raw_events))
    return timeline


# ── Stage 2: Classification + narration ─────────────────────────────────────────

_NARRATE_SYSTEM = textwrap.dedent("""\
    You write a concise thematic headline (1-2 sentences) for someone's workday.
    Base it only on what was DONE — do not mention unfinished tasks or what was missed.
    Be specific: name projects and outcomes, not app names.
    Output ONLY valid JSON: {"day_theme": "..."}
""")


def narrate(
    classification: dict,
    context: dict,
    date: datetime.date,
    unclassified_activities: list[dict] | None = None,
    trace: DailyPipelineTrace | None = None,
) -> dict:
    """
    Generate a day_theme narrative from classification results.

    Parameters
    ----------
    classification          : output of classifier.post_process / classify
    context                 : fetch_context.load() dict
    date                    : the day being summarized
    unclassified_activities : named clusters from activity_namer (PROMPT_14)

    Returns {"day_theme": str}.
    """
    cfg = get_config()
    model: str = cfg.get("anthropic_model", "claude-sonnet-4-6")

    done = classification.get("done") or []
    unfinished = classification.get("unfinished") or []
    unclassified = unclassified_activities or []

    done_lines = [
        f"✓ {t.get('task_name', '')} ({t.get('project', '')})"
        for t in done
    ]
    unfin_lines = [
        f"✗ {t.get('task_name', '')} — {t.get('reason', '')}"
        for t in unfinished
    ]
    unclass_lines = [
        f"? {a.get('suggested_name', a.get('app', 'activity'))}"
        for a in unclassified
    ]

    general = (context.get("general") or "")[:400]
    from collectors.collect_tasks import format_active_projects_prompt

    projects_text = format_active_projects_prompt(
        context.get("active_projects") or [],
    )

    user_msg = (
        f"DATE: {date} ({date.strftime('%A')})\n\n"
        f"=== DONE ({len(done)}) ===\n"
        + ("\n".join(done_lines) if done_lines else "(none)")
        + f"\n\n=== UNFINISHED ({len(unfinished)}) ===\n"
        + ("\n".join(unfin_lines) if unfin_lines else "(none)")
        + f"\n\n=== UNCLASSIFIED ({len(unclassified)}) ===\n"
        + ("\n".join(unclass_lines) if unclass_lines else "(none)")
        + f"\n\n=== ACTIVE PROJECTS (Notion DB) ===\n{projects_text}\n\n"
        f"=== GENERAL CONTEXT ===\n{general or '(none)'}\n\n"
        'Return JSON: {"day_theme": "1-2 sentence headline"}'
    )

    try:
        raw, in_tok, out_tok = _call_claude(
            _NARRATE_SYSTEM, user_msg,
            max_tokens=300, model=model, temperature=0,
            call_type="narrate",
        )
        result = _extract_json(raw)
        if isinstance(result, dict) and result.get("day_theme"):
            log.info("Narrate: %d in + %d out tokens", in_tok, out_tok)
            out = {"day_theme": str(result["day_theme"]).strip()}
            if trace:
                trace.add_stage(
                    "6. Stage 2 — Narrate day theme (Sonnet)",
                    input_data={
                        "classification_summary": {
                            "done_count": len(done),
                            "unfinished_count": len(unfinished),
                            "unclassified_count": len(unclassified),
                        },
                    },
                    system=_NARRATE_SYSTEM,
                    prompt=user_msg,
                    llm_raw=raw,
                    output_data=out,
                )
            return out
    except Exception as exc:
        log.warning("Narrate failed (%s) — using fallback theme", exc)
        if trace:
            trace.add_stage(
                "6. Stage 2 — Narrate day theme (Sonnet)",
                system=_NARRATE_SYSTEM,
                prompt=user_msg,
                notes=f"Failed: {exc}",
            )

    if done:
        names = ", ".join(t.get("task_name", "") for t in done[:3])
        fallback = {"day_theme": f"Completed: {names}."}
    else:
        fallback = {"day_theme": "Mixed day — some tasks remain open."}
    if trace:
        trace.add_stage(
            "6. Stage 2 — Narrate day theme (Sonnet)",
            system=_NARRATE_SYSTEM,
            prompt=user_msg,
            notes="Used fallback theme.",
            output_data=fallback,
        )
    return fallback


# ── Legacy Stage 2: Main summarizer (kept for --test / plan_edit compat) ───────

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a personal productivity assistant with full access to everything the user
    actually did on their computer today. Your job is to:

    1. Write a concise, SEMANTIC daily summary — what was actually accomplished,
       not just which apps were open. Name real topics, decisions, and projects.
    2. Generate a prioritized plan for tomorrow based on today's output,
       unfinished work, and the user's stated goals.

    Rules:
    - Be direct and specific. If the user spent 40 minutes asking an AI about
      system architecture, write "Designed system architecture for X" — not "used Claude.ai".
    - Infer intent from context clues (window titles, text content, meeting topics).
    - For the plan, output 5–7 concrete, actionable tasks ordered by priority.
    - Do NOT mention raw app names or URLs in the summary — translate to meaning.
    - Output ONLY valid JSON matching the schema in the user message. No explanation.
""")

_OUTPUT_SCHEMA = textwrap.dedent("""\
    === OUTPUT SCHEMA ===
    Return ONLY valid JSON — no markdown fences, no explanation:
    {
      "summary": "3-5 paragraph narrative of what was actually accomplished today",
      "highlights": ["key accomplishment 1", "key accomplishment 2", "..."],
      "tomorrow_plan": [
        {"id": 1, "task": "...", "priority": "high|medium|low", "context": "why this task", "project": "project name or ''"},
        ...
      ],
      "time_breakdown": [
        {"app": "Chrome", "minutes": 120, "category": "research|coding|writing|meetings|admin|other"},
        ...
      ],
      "blockers": ["anything that seems stuck or unresolved"]
    }
""")

_PLAN_EDIT_SYSTEM = textwrap.dedent("""\
    You are a plan editor. Given a JSON plan and a plain-English edit request,
    return the updated plan as a JSON array. Same schema as input. No explanation.
    Output ONLY the JSON array.
""")


def _render_segment(seg: dict, typing_limit: int) -> str:
    lines: list[str] = []
    if seg.get("synthetic"):
        lines.append(f"[Cursor session — workspace: {seg['app'].replace('Cursor/', '')}]")
    else:
        lines.append(
            f"[{seg['start']}–{seg['end']}] {seg['app']} — {seg['duration_minutes']} min"
        )

    cursor_summary = seg.get("cursor_summary")
    typing = seg.get("typing", [])
    capture_mode = seg.get("capture_mode", "charcount")

    if cursor_summary:
        lines.append(f"  Coding session: {cursor_summary}")
    elif typing and capture_mode == "full":
        lines.append("  Typed:")
        for t in typing[:typing_limit]:
            lines.append(f"  • {t}")
    elif capture_mode in ("summary", "charcount"):
        lines.append("  [content not captured for this app]")
    else:
        lines.append("  [reading / browsing — no typed input]")

    return "\n".join(lines)


def _render_timeline_section(timeline: list[dict], max_timeline_tokens: int) -> str:
    """
    Render the chronological section, applying budget cuts if needed.

    Priority order for removal (never remove Cursor segments):
      1. charcount-mode non-Cursor segments
      2. Truncate typing to 3 entries per segment
      3. summary-mode segments
      4. Truncate to 2 entries (domain-filter segments already at 3)
    """
    cfg = get_config()
    max_typing = cfg.get("max_typing_per_segment", 6)
    max_chars = max_timeline_tokens * 4

    def _render_all(tl: list[dict], typing_limit: int) -> str:
        return "\n\n".join(_render_segment(s, typing_limit) for s in tl)

    full = _render_all(timeline, max_typing)
    if len(full) <= max_chars:
        return full

    # Cut 1: drop charcount-only, non-Cursor segments
    is_cursor = lambda s: "Cursor" in s["app"] or s.get("cursor_summary")
    reduced = [
        s for s in timeline
        if is_cursor(s) or s.get("capture_mode") != "charcount"
    ]
    dropped = len(timeline) - len(reduced)
    if dropped:
        log.warning("Timeline budget: dropped %d charcount segment(s)", dropped)
    text = _render_all(reduced, max_typing)
    if len(text) <= max_chars:
        return text

    # Cut 2: truncate typing to 3
    log.warning("Timeline budget: truncating typing lists to 3 entries")
    text = _render_all(reduced, 3)
    if len(text) <= max_chars:
        return text

    # Cut 3: drop summary-mode segments
    reduced2 = [
        s for s in reduced
        if is_cursor(s) or s.get("capture_mode") != "summary"
    ]
    dropped2 = len(reduced) - len(reduced2)
    if dropped2:
        log.warning("Timeline budget: dropped %d summary-mode segment(s)", dropped2)
    text = _render_all(reduced2, 3)
    if len(text) <= max_chars:
        return text

    # Cut 4: typing to 2
    log.warning("Timeline budget: truncating typing lists to 2 entries")
    text = _render_all(reduced2, 2)
    if len(text) <= max_chars:
        return text

    # Last resort: hard truncate
    log.warning(
        "Timeline budget: hard truncating to %d chars (was %d)", max_chars, len(text)
    )
    return text[:max_chars] + "\n[... timeline truncated for token budget ...]"


def _render_micro_section(micro_summaries: list[dict]) -> str:
    """Render micro-summaries as a compact text block for the prompt."""
    lines: list[str] = []
    for m in micro_summaries:
        ws = m.get("window_start", "")
        we = m.get("window_end", "")
        # e.g. "10:00–10:30"
        ws_hm = ws[11:16] if len(ws) >= 16 else ws[:5]
        we_hm = we[11:16] if len(we) >= 16 else we[:5]
        app   = m.get("app", "?")
        mins  = int(m.get("minutes", 0))
        summ  = m.get("summary", "")
        lines.append(f"[{ws_hm}–{we_hm}] {app} ({mins}m): {summ}")
    return "\n".join(lines)


def render_active_projects_section(active_projects: list[dict]) -> str:
    """Render active Notion projects for LLM prompts."""
    from collectors.collect_tasks import format_active_projects_prompt
    return format_active_projects_prompt(active_projects)


def _render_projects_section(active_projects: list[dict]) -> str:
    return render_active_projects_section(active_projects)


def _render_tasks_section(today_tasks: list[dict]) -> str:
    """Render today's task list as bullet text for the prompt."""
    if not today_tasks:
        return "No tasks scheduled for today in the Tasks DB."
    lines: list[str] = []
    for t in today_tasks:
        pri  = t.get("priority", "")
        name = t.get("task_name", "")
        est  = t.get("estimated_minutes")
        proj = t.get("project", "")
        est_str  = f" (est. {est} min)" if est else ""
        proj_str = f" — {proj}" if proj else ""
        lines.append(f"• [{pri}] {name}{est_str}{proj_str}")
    return "\n".join(lines)


def build_prompt(
    timeline: list[dict],
    meetings: list[dict],
    context: dict,
    date: datetime.date,
    micro_summaries: list[dict] | None = None,
    compress: bool = False,
) -> str:
    """
    Construct the user-turn prompt for the main summarizer call.

    Parameters
    ----------
    timeline        : output of build_timeline()
    meetings        : list from collect_notion_meetings.get_date()
    context         : New schema:
                        {
                          "general":         str,
                          "today":           {"plan": str, "actual": str} | None,
                          "active_projects": list[dict],   # from Projects DB
                          "today_tasks":     list[dict],   # from Tasks DB
                        }
                      Also accepts the old schema (without active_projects /
                      today_tasks) for backward compatibility.
    date            : the day being summarized
    micro_summaries : optional list of 30-min window summaries; when provided
                      these replace the detailed typing analysis (token reduction).
    compress        : When True, check the total prompt token count after all
                      sections are rendered.  If it exceeds
                      config["max_synthesis_tokens"] (default 12 000), run a
                      second-pass haiku call that compresses each content
                      section (day timeline, meetings, general context) down to
                      at most 3 bullet points before the final synthesis call.
                      Tasks and Projects sections are never compressed (they are
                      already structured and concise).
    """
    cfg = get_config()
    max_context_chars   = cfg.get("max_context_tokens", 600) * 4
    max_meeting_chars   = cfg.get("max_meeting_tokens", 800) * 4
    max_timeline_tokens = cfg.get("max_timeline_tokens", 3000)
    # Per-source compression limit — any source blob above this gets a haiku
    # pre-compression pass before it feeds into the main synthesis prompt.
    max_per_source: int = cfg.get("max_tokens_per_source", MAX_TOKENS_PER_SOURCE)
    fast_model: str = cfg.get(
        "anthropic_cursor_presummary_model", "claude-haiku-4-5-20251001"
    )

    day_of_week = date.strftime("%A")

    # ── Context: new structured schema or fallback to legacy free-text ─────────
    # Compress general context if it's very large before the hard char-limit slice.
    raw_general = context.get("general") or ""
    raw_general = _maybe_compress(raw_general, "general_context", max_per_source, fast_model)
    general_ctx = raw_general[:max_context_chars]
    active_projects: list[dict] = context.get("active_projects") or []
    today_tasks:     list[dict] = context.get("today_tasks") or []

    # Legacy fallback: if no today_tasks, try today.plan from old schema
    today_ctx  = context.get("today") or {}
    today_plan_legacy = today_ctx.get("plan", "") if today_ctx else ""

    # Build context sections
    projects_text = _render_projects_section(active_projects)
    if today_tasks:
        tasks_text = _render_tasks_section(today_tasks)
    elif today_plan_legacy:
        # Old schema: pre-formatted plan string
        tasks_text = today_plan_legacy
    else:
        tasks_text = "No tasks scheduled for today in the Tasks DB."

    # ── Meetings section ───────────────────────────────────────────────────────
    meeting_lines: list[str] = []
    for m in meetings:
        time_str = m.get("time", "")
        title    = m.get("title", "Untitled")
        raw_summary = (m.get("summary") or "").strip()
        header   = f"[{time_str}] {title}" if time_str else title
        meeting_lines.append(f"\n{header}")
        if raw_summary:
            # Compress the individual meeting summary if it's very long before
            # applying the per-meeting `:500` char cap.  This ensures the most
            # important content survives rather than just the first 500 chars.
            summary = _maybe_compress(
                raw_summary,
                f"meeting:{title[:40]}",
                max_per_source,
                fast_model,
            )
            meeting_lines.append(summary[:500])
    meetings_text = "".join(meeting_lines).strip()
    if len(meetings_text) > max_meeting_chars:
        meetings_text = meetings_text[:max_meeting_chars] + "\n[... truncated ...]"
    if not meetings_text:
        meetings_text = "No conversations logged today."

    # ── Day section: micro-summaries (token-saving) or full timeline ───────────
    if micro_summaries:
        micro_text = _render_micro_section(micro_summaries)
        light_tl   = [{**s, "typing": []} for s in timeline]
        tl_text    = _render_timeline_section(light_tl, max_timeline_tokens // 2)
        day_section = (
            "=== ACTIVITY SUMMARIES (30-MIN WINDOWS) ===\n"
            f"{micro_text}\n"
            "\n"
            "=== TIME ALLOCATION (from activity tracking) ===\n"
            f"{tl_text}"
        )
    else:
        timeline_text = _render_timeline_section(timeline, max_timeline_tokens)
        day_section   = (
            "=== YOUR DAY — CHRONOLOGICAL ===\n"
            f"{timeline_text}"
        )

    # ── Second-pass compression (--compress flag) ─────────────────────────────
    # Activated only when compress=True.  Measures the total rendered content
    # token count and, if it exceeds the threshold, compresses each of the three
    # heavy sections (day timeline, meetings, general context) to ≤ 3 bullets
    # each using the fast haiku model.  Tasks and Projects are left intact.
    if compress:
        threshold: int = cfg.get("max_synthesis_tokens", MAX_SYNTHESIS_TOKENS)
        sizes: dict[str, int] = {
            "day":             _estimate_tokens(day_section),
            "meetings":        _estimate_tokens(meetings_text),
            "general_context": _estimate_tokens(general_ctx),
            "tasks":           _estimate_tokens(tasks_text),
            "projects":        _estimate_tokens(projects_text),
        }
        total_tokens = sum(sizes.values())

        if total_tokens > threshold:
            log.warning(
                "Second-pass compression triggered: ~%d tokens total > threshold %d\n"
                "  Section breakdown: %s",
                total_tokens, threshold,
                "  ".join(
                    f"{k}={v}"
                    for k, v in sorted(sizes.items(), key=lambda x: -x[1])
                ),
            )
            day_section   = _compress_to_bullets(day_section,   "day_timeline",    3, fast_model)
            meetings_text = _compress_to_bullets(meetings_text, "meetings",         3, fast_model)
            general_ctx   = _compress_to_bullets(general_ctx,   "general_context",  3, fast_model)
            log.info(
                "Second-pass complete: ~%d tokens → ~%d tokens after compression",
                total_tokens,
                sum(_estimate_tokens(t) for t in (day_section, meetings_text, general_ctx))
                + sizes["tasks"] + sizes["projects"],
            )
        else:
            log.info(
                "Second-pass compression not needed: ~%d tokens (threshold=%d)",
                total_tokens, threshold,
            )

    return (
        f"TODAY'S DATE: {date} ({day_of_week})\n"
        "\n"
        "=== YOUR ACTIVE PROJECTS ===\n"
        f"{projects_text}\n"
        "\n"
        "=== TODAY'S PLANNED TASKS ===\n"
        f"{tasks_text}\n"
        "\n"
        "=== GENERAL CONTEXT ===\n"
        f"{general_ctx or '(no general context provided)'}\n"
        "\n"
        f"{day_section}\n"
        "\n"
        "=== CONVERSATIONS TODAY ===\n"
        f"{meetings_text}\n"
        "\n"
        f"{_OUTPUT_SCHEMA}"
    )


def summarize(
    typing_entries: list[dict],
    activitywatch: dict,
    cursor_sessions: list[dict],
    meetings: list[dict],
    context: dict,
    date: datetime.date,
    compress: bool = False,
    trace: DailyPipelineTrace | None = None,
) -> dict:
    """
    Run the full pipeline: timeline → classify → narrate.

    Stage 1A: pre-summarize each Cursor session (haiku model)
    Stage 1B: build chronological timeline
    Stage 1C: classify activity against Notion tasks
    Stage 2:  narrate day_theme from classification

    On hard failure returns {"error": str, ...empty fields...}.
    """
    _ = compress  # legacy CLI flag — classification path ignores it

    stage1a_tokens = 0

    log.info("=== Stage 1A: Cursor pre-summarizer (%d sessions) ===", len(cursor_sessions))
    cursor_presummaries = presummary_all_cursor(cursor_sessions)
    stage1a_tokens = sum(p.get("token_count", 0) for p in cursor_presummaries)

    if trace:
        trace.add_stage(
            "2. Stage 1A — Cursor pre-summary (Haiku)",
            input_data={
                "session_count": len(cursor_sessions),
                "sessions": [
                    {
                        "session_id": s.get("session_id", "")[:16],
                        "workspace": s.get("workspace"),
                        "turn_count": s.get("turn_count", len(s.get("user_turns", []))),
                    }
                    for s in cursor_sessions
                ],
            },
            output_data=cursor_presummaries,
        )

    log.info("=== Stage 1B: Build timeline ===")
    timeline = build_timeline(typing_entries, activitywatch, cursor_presummaries)

    if trace:
        trace.add_stage(
            "3. Stage 1B — Timeline (AW + typing + cursor joined)",
            input_data={
                "typing_entry_count": len(typing_entries),
                "raw_event_count": len((activitywatch or {}).get("raw_events") or []),
                "presummary_count": len(cursor_presummaries),
            },
            output_data=timeline,
        )

    log.info("=== Stage 1C: Classify tasks ===")
    from collectors.collect_tasks import get_classifiable_tasks
    from pipeline.classifier import classify as do_classify

    tasks = get_classifiable_tasks(date)
    if not tasks:
        tasks = context.get("today_tasks") or []

    try:
        classification = do_classify(
            timeline,
            tasks,
            meetings,
            active_projects=context.get("active_projects") or [],
            trace=trace,
        )
    except Exception as exc:
        log.error("Classification failed: %s", exc)
        return {
            "error": str(exc),
            "day_theme": "",
            "done": [], "unfinished": [],
            "unclassified_activities": [],
            "unmatched_segments": [],
            "summary": "", "highlights": [],
            "tomorrow_plan": [], "time_breakdown": [], "blockers": [],
        }

    unmatched = classification.get("unmatched_segments") or []
    from pipeline import activity_namer
    unclassified_activities = activity_namer.name_unclassified(
        unmatched, trace=trace,
    )

    log.info("=== Stage 2: Narrate day theme ===")
    narration = narrate(
        classification, context, date, unclassified_activities, trace=trace,
    )

    time_breakdown: list[dict] = []
    for item in (activitywatch.get("by_app") or [])[:6]:
        time_breakdown.append({
            "app": item.get("app", "?"),
            "minutes": item.get("minutes", 0),
            "category": item.get("category", "other"),
        })

    result: dict = {
        "day_theme": narration.get("day_theme", ""),
        "done": classification.get("done") or [],
        "unfinished": classification.get("unfinished") or [],
        "unclassified_activities": unclassified_activities,
        "unmatched_segments": unmatched,
        "time_breakdown": time_breakdown,
        "summary": narration.get("day_theme", ""),
        "highlights": [t.get("task_name", "") for t in classification.get("done") or []],
        "tomorrow_plan": [],
        "blockers": [t.get("reason", "") for t in classification.get("unfinished") or []],
    }

    log.info(
        "Summarize done: %d done, %d unfinished, %d unclassified  "
        "(Stage1A: %d tokens)",
        len(result["done"]),
        len(result["unfinished"]),
        len(result["unclassified_activities"]),
        stage1a_tokens,
    )
    return result


# ── Plan edit ─────────────────────────────────────────────────────────────────

def parse_plan_edit(current_plan: list[dict], edit_request: str) -> list[dict]:
    """
    Apply *edit_request* (plain English) to *current_plan* via a small Claude call.

    Returns the updated plan list (same schema as tomorrow_plan).
    Raises RuntimeError on API or parse failure.
    """
    cfg = get_config()
    model: str = cfg.get(
        "anthropic_cursor_presummary_model",
        cfg.get("anthropic_model", "claude-haiku-4-5-20251001"),
    )
    max_tokens: int = cfg.get("anthropic_cursor_presummary_max_tokens", 400)

    plan_json = json.dumps(current_plan, ensure_ascii=False)
    user_msg = (
        f"Current plan:\n{plan_json}\n\n"
        f'Edit request: "{edit_request}"\n\n'
        "Return the updated plan as a JSON array. Same schema. No explanation."
    )

    try:
        raw, _, _ = _call_claude(
            _PLAN_EDIT_SYSTEM, user_msg, max_tokens, model,
            call_type="plan_edit",
        )
    except anthropic.APIError as exc:
        raise RuntimeError(f"Claude API error during plan edit: {exc}") from exc

    try:
        updated = _extract_json(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse updated plan JSON: {exc}\nRaw: {raw[:300]}"
        ) from exc

    if not isinstance(updated, list):
        raise RuntimeError(f"Expected a JSON array from plan edit, got: {type(updated)}")

    log.info("Plan edit applied: %d tasks", len(updated))
    return updated


# ── CLI ───────────────────────────────────────────────────────────────────────

def _load_fixture() -> dict:
    """Load the test fixture from tests/fixture_day.json."""
    fixture_path = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixture_day.json"
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found: {fixture_path}")
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    # Parse date string → datetime.date
    raw["date"] = datetime.date.fromisoformat(raw["date"])
    return raw


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Summarizer pipeline test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run end-to-end using tests/fixture_day.json",
    )
    parser.add_argument(
        "--stage1-only", action="store_true",
        help="Run Stage 1A + 1B only (no main Claude call)",
    )
    parser.add_argument(
        "--show-prompt", action="store_true",
        help="Print the final Stage-2 prompt and exit (no Claude call)",
    )
    parser.add_argument(
        "--out", metavar="FILE",
        help="Write JSON result to file",
    )
    parser.add_argument(
        "--compress", action="store_true",
        help=(
            "Enable second-pass compression before Stage 2. "
            "If the assembled prompt exceeds config['max_synthesis_tokens'] "
            f"(default {MAX_SYNTHESIS_TOKENS}), each content section is reduced "
            "to ≤3 bullet points. Use to compare output quality against the "
            "baseline on heavy days."
        ),
    )
    args = parser.parse_args()

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    console = Console()

    if args.test:
        try:
            fixture = _load_fixture()
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/]")
            sys.exit(1)

        date = fixture["date"]
        typing_entries = fixture.get("typing", [])
        activitywatch = fixture.get("activitywatch", {})
        cursor_sessions = fixture.get("cursor_sessions", [])
        meetings = fixture.get("meetings", [])
        context = fixture.get("context", {})

        console.print(f"\n[bold cyan]Fixture date:[/] {date}")
        console.print(
            f"  typing={len(typing_entries)}  "
            f"raw_events={len(activitywatch.get('raw_events', []))}  "
            f"cursor_sessions={len(cursor_sessions)}  "
            f"meetings={len(meetings)}"
        )

        # Stage 1A
        console.print("\n[bold]Stage 1A — Cursor pre-summarizer[/]")
        presummaries = presummary_all_cursor(cursor_sessions)
        for p in presummaries:
            console.print(Panel(
                p["summary"],
                title=f"[cyan]{p['workspace']}[/]  {p['started_at'][:16]}  "
                      f"[dim](tokens: {p['token_count']})[/]",
                expand=False,
            ))

        # Stage 1B
        console.print("\n[bold]Stage 1B — Timeline[/]")
        timeline = build_timeline(typing_entries, activitywatch, presummaries)
        t = Table(show_header=True, header_style="bold magenta")
        t.add_column("Time", style="cyan", no_wrap=True)
        t.add_column("App")
        t.add_column("Min", justify="right")
        t.add_column("Mode")
        t.add_column("Typing / Cursor summary", overflow="fold")
        for seg in timeline:
            time_label = f"{seg['start']}–{seg['end']}"
            if seg.get("synthetic"):
                time_label = "[synthetic]"
            content = ""
            if seg.get("cursor_summary"):
                content = seg["cursor_summary"][:80]
            elif seg.get("typing"):
                content = seg["typing"][0][:80]
            t.add_row(
                time_label,
                seg["app"],
                str(seg["duration_minutes"]),
                seg["capture_mode"],
                content,
            )
        console.print(t)

        if args.stage1_only:
            console.print("\n[dim]--stage1-only: stopping before Stage 2.[/]")
            return

        # Build prompt (pass compress flag so --show-prompt reflects final form)
        prompt = build_prompt(timeline, meetings, context, date, compress=args.compress)
        compress_note = "  [yellow](--compress active)[/]" if args.compress else ""
        console.print(
            f"\n[dim]Prompt: {len(prompt)} chars (~{len(prompt)//4} tokens)[/]{compress_note}"
        )

        if args.show_prompt:
            console.print(Panel(prompt, title="[bold]Final prompt[/]", expand=False))
            return

        # Stage 2
        console.print("\n[bold]Stage 2 — Main summarizer[/]")
        result = summarize(
            typing_entries=typing_entries,
            activitywatch=activitywatch,
            cursor_sessions=cursor_sessions,
            meetings=meetings,
            context=context,
            date=date,
            compress=args.compress,
        )

        if "error" in result:
            console.print(f"[red]Error: {result['error']}[/]")
            sys.exit(1)

        console.print("\n[bold green]Summary:[/]")
        console.print(result.get("summary", ""))

        console.print("\n[bold]Highlights:[/]")
        for h in result.get("highlights", []):
            console.print(f"  • {h}")

        console.print("\n[bold]Tomorrow's plan:[/]")
        for task in result.get("tomorrow_plan", []):
            pri = task.get("priority", "?").upper()
            console.print(
                f"  [{pri}] {task.get('task', '')}  — {task.get('context', '')}"
            )

        if result.get("blockers"):
            console.print("\n[bold yellow]Blockers:[/]")
            for b in result["blockers"]:
                console.print(f"  ⚠ {b}")

        if args.out:
            pathlib.Path(args.out).write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            console.print(f"\n[dim]Result written to {args.out}[/]")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
