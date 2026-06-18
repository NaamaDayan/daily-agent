"""
Task classifier — Stage 1 of the daily pipeline.

Matches timeline segments and meetings to Notion tasks using observable evidence.
Confidence thresholds are applied deterministically in post_process().

Public API
----------
classify(timeline, tasks, meetings, active_projects=None) -> dict
post_process(raw, tasks) -> dict
build_classification_prompt(timeline, tasks, meetings, active_projects=None) -> str

CLI
---
    python pipeline/classifier.py --test
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import textwrap
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipeline.daily_trace import DailyPipelineTrace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import anthropic

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("classifier")

MIN_SEGMENT_SECONDS = 180  # ignore segments < 3 min

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a strict productivity classifier. Match computer activity to tasks
    using only observable evidence. Never speculate.
    Rules:

    Score each task independently against the full timeline.
    A segment may be cited as evidence for more than one task if genuine
    signal exists for both — do not artificially exclude segments.
    Output a confidence_score 0.0–1.0 per task based solely on evidence
    found in the timeline or meetings.
    For tasks with target_count > 1: count distinct matching instances
    separately. confidence_score = instances_found / instances_required,
    capped at 1.0.
    No digital footprint = confidence_score 0.0. Do not infer offline work.
    Ignore segments shorter than 3 minutes.
    Output ONLY the JSON. No prose.
""")

_OUTPUT_SCHEMA = textwrap.dedent("""\
    === OUTPUT SCHEMA ===
    Return ONLY valid JSON — no markdown fences, no explanation:
    {
      "tasks": [
        {
          "page_id": "notion page id",
          "task_name": "task name",
          "project": "project name or empty string",
          "confidence_score": 0.0,
          "instances_found": 0,
          "instances_required": 1,
          "evidence": ["brief evidence string"],
          "time_ranges": ["HH:MM–HH:MM"]
        }
      ]
    }
""")


def _get_thresholds() -> tuple[float, float]:
    cfg = get_config()
    done = float(cfg.get("classification_done_threshold", 0.80))
    minimum = float(cfg.get("classification_min_threshold", 0.40))
    return done, minimum


def _segment_key(seg: dict) -> str:
    """Stable key for a timeline segment: start–end time range."""
    return f"{seg.get('start', '')}–{seg.get('end', '')}"


def _filter_timeline(timeline: list[dict]) -> list[dict]:
    """Keep only segments >= MIN_SEGMENT_SECONDS (3 min)."""
    return [
        seg for seg in timeline
        if seg.get("duration_minutes", 0) * 60 >= MIN_SEGMENT_SECONDS
    ]


def _segment_content_line(seg: dict) -> str:
    if seg.get("cursor_summary"):
        return seg["cursor_summary"]
    typing = seg.get("typing") or []
    if typing:
        cfg = get_config()
        limit = int(cfg.get("max_typing_per_segment", 6))
        lines = [t[:500] for t in typing[:limit]]
        if len(typing) > limit:
            lines.append(f"[... {len(typing) - limit} more typing snippet(s)]")
        return "\n".join(f"• {line}" for line in lines)
    return "[no content]"


def _segment_to_unmatched(seg: dict) -> dict:
    app = seg.get("app", "")
    domain = seg.get("domain")
    return {
        "start": seg.get("start", ""),
        "end": seg.get("end", ""),
        "duration_minutes": int(seg.get("duration_minutes", 0)),
        "app": app,
        "domain": domain,
        "typing": list(seg.get("typing") or []),
        "cursor_summary": seg.get("cursor_summary"),
    }


def build_classification_prompt(
    timeline: list[dict],
    tasks: list[dict],
    meetings: list[dict],
    active_projects: list[dict] | None = None,
) -> str:
    """Build the user-turn prompt for the classifier LLM call."""
    filtered = _filter_timeline(timeline)

    task_lines: list[str] = []
    for i, t in enumerate(tasks, 1):
        notes = (t.get("notes") or "").strip() or "none provided"
        target = t.get("target_count", 1)
        task_lines.append(
            f'[{i}] page_id={t.get("page_id", "")} '
            f'"{t.get("task_name", "")}" | Project: {t.get("project", "")} '
            f"| Target: {target}x\n"
            f"Notes/fingerprint: {notes}"
        )

    seg_lines: list[str] = []
    for seg in filtered:
        app = seg.get("app", "?")
        domain = seg.get("domain")
        app_label = f"{app}/{domain}" if domain and "/" not in app else app
        key = _segment_key(seg)
        content = _segment_content_line(seg)
        seg_lines.append(
            f"[{key}] {app_label} — {seg.get('duration_minutes', 0)}min\n"
            f"{content}"
        )

    if meetings:
        meeting_lines: list[str] = []
        for m in meetings:
            time_str = m.get("time", "")
            title = m.get("title", "Untitled")
            summary = (m.get("summary") or "")[:250]
            meeting_lines.append(f"[{time_str}] {title}: {summary}")
        meetings_text = "\n".join(meeting_lines)
    else:
        meetings_text = "None"

    from pipeline.learning_store import format_few_shot_for_prompt, get_few_shot_examples

    few_shot_text = format_few_shot_for_prompt(get_few_shot_examples())
    few_shot_block = f"{few_shot_text}\n\n" if few_shot_text else ""

    from collectors.collect_tasks import format_active_projects_prompt

    projects_text = format_active_projects_prompt(active_projects or [])

    return (
        few_shot_block
        + "=== ACTIVE PROJECTS (Notion Projects DB) ===\n"
        + projects_text
        + "\n\n=== TASKS ===\n"
        + "\n".join(task_lines)
        + "\n\n=== TIMELINE (segments ≥ 3 min) ===\n"
        + ("\n".join(seg_lines) if seg_lines else "(no qualifying segments)")
        + "\n\n=== MEETINGS ===\n"
        + meetings_text
        + "\n\n"
        + _OUTPUT_SCHEMA
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end]).strip()
    return json.loads(text)


def _call_claude(system: str, user: str) -> tuple[str, int, int]:
    cfg = get_config()
    model: str = cfg.get("anthropic_model", "claude-sonnet-4-6")
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    in_tok = msg.usage.input_tokens
    out_tok = msg.usage.output_tokens
    try:
        from utils.cost_logger import log_api_call
        log_api_call(model, in_tok, out_tok, "stage1_classify")
    except Exception:
        pass
    return msg.content[0].text, in_tok, out_tok


def post_process(raw: dict, tasks: list[dict], filtered_timeline: list[dict]) -> dict:
    """
    Apply confidence thresholds deterministically after LLM classification.

    Buckets tasks into done/unfinished.  Computes unmatched_segments in Python
    as any segment not claimed by at least one task that reached min_threshold —
    independent of what the LLM returned for unmatched.
    """
    done_threshold, min_threshold = _get_thresholds()

    task_by_id = {t["page_id"]: t for t in tasks}
    llm_tasks = raw.get("tasks") or []
    llm_by_id = {item.get("page_id"): item for item in llm_tasks if item.get("page_id")}

    done: list[dict] = []
    unfinished: list[dict] = []
    # Segments claimed by at least one task that hit min_threshold.
    claimed_keys: set[str] = set()

    def _task_meta(page_id: str, item: dict | None) -> dict:
        base = task_by_id.get(page_id, {})
        required = int(
            (item or {}).get("instances_required")
            or base.get("target_count")
            or 1
        )
        found = int((item or {}).get("instances_found") or 0)
        return {
            "task_name": (item or {}).get("task_name") or base.get("task_name", ""),
            "page_id": page_id,
            "project": (item or {}).get("project") or base.get("project", ""),
            "instances_found": found,
            "instances_required": required,
        }

    for page_id in task_by_id:
        item = llm_by_id.get(page_id)
        if item is None:
            meta = _task_meta(page_id, None)
            unfinished.append({
                **meta,
                "confidence_score": 0.0,
                "reason": "No digital footprint detected",
            })
            continue

        score = float(item.get("confidence_score", 0.0))
        meta = _task_meta(page_id, item)
        time_ranges = list(item.get("time_ranges") or [])

        if score >= done_threshold:
            done.append({
                **meta,
                "confidence_score": score,
                "evidence": list(item.get("evidence") or []),
                "time_ranges": time_ranges,
            })
            claimed_keys.update(time_ranges)
        elif score >= min_threshold:
            if meta["instances_required"] > 1:
                reason = (
                    f"{meta['instances_found']}/{meta['instances_required']} "
                    "instances found"
                )
            else:
                reason = f"Partial evidence (confidence {score:.0%})"
            unfinished.append({
                **meta,
                "confidence_score": score,
                "reason": reason,
            })
            claimed_keys.update(time_ranges)
        else:
            unfinished.append({
                **meta,
                "confidence_score": score,
                "reason": "No digital footprint detected",
            })

    # Compute unmatched from the full timeline — any segment not claimed by a
    # task that reached min_threshold is unmatched.
    final_unmatched: list[dict] = [
        _segment_to_unmatched(s)
        for s in filtered_timeline
        if _segment_key(s) not in claimed_keys
    ]

    return {
        "done": done,
        "unfinished": unfinished,
        "unmatched_segments": final_unmatched,
    }


def classify(
    timeline: list[dict],
    tasks: list[dict],
    meetings: list[dict],
    active_projects: list[dict] | None = None,
    trace: DailyPipelineTrace | None = None,
) -> dict:
    """
    Classify timeline activity against tasks via Claude, then apply thresholds.

    Returns post_process() output: done, unfinished, unmatched_segments.
    """
    if not tasks:
        filtered = _filter_timeline(timeline)
        result = {
            "done": [],
            "unfinished": [],
            "unmatched_segments": [_segment_to_unmatched(s) for s in filtered],
        }
        if trace:
            trace.add_stage(
                "4. Stage 1C — Task classification (Sonnet)",
                notes="No classifiable tasks — all segments unmatched.",
                output_data=result,
            )
        return result

    prompt = build_classification_prompt(
        timeline, tasks, meetings, active_projects=active_projects,
    )
    log.info(
        "Classifier prompt: ~%d tokens, %d tasks, %d timeline segments",
        len(prompt) // 4,
        len(tasks),
        len(_filter_timeline(timeline)),
    )

    raw_text = ""
    raw: dict = {}
    try:
        raw_text, in_tok, out_tok = _call_claude(_SYSTEM_PROMPT, prompt)
        log.info("Classifier API: %d in + %d out tokens", in_tok, out_tok)
        raw = _extract_json(raw_text)
    except anthropic.APIError as exc:
        log.error("Classifier API error: %s", exc)
        if trace:
            trace.add_stage(
                "4. Stage 1C — Task classification (Sonnet)",
                input_data={"tasks": tasks},
                prompt=prompt,
                system=_SYSTEM_PROMPT,
                notes=f"API error: {exc}",
            )
        raise
    except json.JSONDecodeError as exc:
        log.error("Classifier JSON parse failed: %s", exc)
        if trace:
            trace.add_stage(
                "4. Stage 1C — Task classification (Sonnet)",
                input_data={"tasks": tasks},
                prompt=prompt,
                system=_SYSTEM_PROMPT,
                llm_raw=raw_text[:5000] if raw_text else None,
                notes=f"JSON parse error: {exc}",
            )
        raise ValueError(f"Classifier returned non-JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"Classifier expected JSON object, got {type(raw)}")

    result = post_process(raw, tasks, _filter_timeline(timeline))
    if trace:
        trace.add_stage(
            "4. Stage 1C — Task classification (Sonnet)",
            input_data={
                "tasks": tasks,
                "timeline_segments": len(_filter_timeline(timeline)),
                "meetings_count": len(meetings),
            },
            system=_SYSTEM_PROMPT,
            prompt=prompt,
            llm_raw=raw,
            output_data=result,
        )
    return result


# ── Test fixture ──────────────────────────────────────────────────────────────

def _test_fixture() -> tuple[list[dict], list[dict], list[dict]]:
    timeline = [
        {
            "start": "09:00",
            "end": "09:45",
            "duration_minutes": 45,
            "app": "Google Chrome/claude.ai",
            "domain": "claude.ai",
            "typing": ["research TAM sizing for enterprise SaaS market"],
            "cursor_summary": None,
        },
        {
            "start": "10:00",
            "end": "10:32",
            "duration_minutes": 32,
            "app": "Google Meet",
            "domain": None,
            "typing": [],
            "cursor_summary": None,
        },
        {
            "start": "11:00",
            "end": "11:18",
            "duration_minutes": 18,
            "app": "Notion",
            "domain": None,
            "typing": [],
            "cursor_summary": None,
        },
    ]
    tasks = [
        {
            "page_id": "task-discovery-001",
            "task_name": "Discovery Call",
            "project": "Sales",
            "notes": "30-min call with prospect about product fit",
            "target_count": 1,
            "recurrence": "None",
        },
        {
            "page_id": "task-tam-002",
            "task_name": "Investigate TAM",
            "project": "Research",
            "notes": "Size the total addressable market for our ICP",
            "target_count": 1,
            "recurrence": "None",
        },
    ]
    meetings = [
        {
            "time": "10:00",
            "title": "Discovery Call — Acme Corp",
            "summary": "Intro call with VP Engineering. Discussed pain points and timeline.",
        },
    ]
    return timeline, tasks, meetings


def _run_test() -> None:
    timeline, tasks, meetings = _test_fixture()
    print("Running classifier --test fixture…")
    print(f"  {len(timeline)} segments, {len(tasks)} tasks, {len(meetings)} meeting(s)\n")

    result = classify(timeline, tasks, meetings)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    classified_ids = {t["page_id"] for t in result["done"] + result["unfinished"]}
    assert len(classified_ids) == len(tasks), (
        f"Expected {len(tasks)} tasks classified, got {len(classified_ids)}"
    )

    print(
        f"\n✓ {len(result['done'])} done, "
        f"{len(result['unfinished'])} unfinished, "
        f"{len(result['unmatched_segments'])} unmatched segment(s)"
    )


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Task classifier")
    parser.add_argument(
        "--test", action="store_true",
        help="Run built-in fixture and print JSON output",
    )
    args = parser.parse_args()
    if args.test:
        _run_test()
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
