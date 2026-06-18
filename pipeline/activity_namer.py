"""
Unclassified activity namer — clusters unmatched timeline segments and names them.

Public API
----------
name_unclassified(unmatched_segments) -> list[dict]

CLI
---
    python pipeline/activity_namer.py --test
"""

from __future__ import annotations

import argparse
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

log = get_logger("activity_namer")

STOPWORDS = frozenset({
    "that", "this", "with", "from", "have", "what", "when",
    "where", "which", "there", "their", "would", "could", "about",
})

_SYSTEM_PROMPT = textwrap.dedent("""\
    Name unclassified work sessions concisely. Each name should be 3-6 words,
    action-oriented, and specific to the content. Use the typing content and
    app/domain as your primary signals.
    Output ONLY a valid JSON array, one object per cluster, in the same order.
    Rules for JSON: use standard double quotes only; no newlines inside strings;
    keep each description under 80 characters; return exactly one object per cluster.
""")


def _sanitize_for_prompt(text: str, max_len: int = 150) -> str:
    """Collapse whitespace and truncate so typing samples don't break JSON output."""
    collapsed = " ".join(text.split())
    if len(collapsed) > max_len:
        collapsed = collapsed[: max_len - 3] + "..."
    return collapsed.replace('"', "'")


def _time_to_minutes(hhmm: str) -> int:
    if not hhmm or ":" not in hhmm:
        return 0
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        return 0
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return 0


def _cluster_key(seg: dict) -> tuple[str, str | None]:
    """Normalize (app, domain) for same-app clustering."""
    domain = seg.get("domain")
    app = seg.get("app", "")
    if domain:
        suffix = f"/{domain}"
        if app.endswith(suffix):
            app = app[: -len(suffix)]
    elif "/" in app:
        app = app.split("/", 1)[0]
    return (app, domain)


def _gap_minutes(end_hhmm: str, start_hhmm: str) -> int:
    return _time_to_minutes(start_hhmm) - _time_to_minutes(end_hhmm)


def _cluster_bounds(cluster: list[dict]) -> tuple[int, int]:
    starts = [_time_to_minutes(s.get("start", "")) for s in cluster]
    ends = [_time_to_minutes(s.get("end", "")) for s in cluster]
    return min(starts), max(ends)


def _clusters_within_minutes(c1: list[dict], c2: list[dict], max_gap: int) -> bool:
    s1, e1 = _cluster_bounds(c1)
    s2, e2 = _cluster_bounds(c2)
    if e1 <= s2:
        gap = s2 - e1
    elif e2 <= s1:
        gap = s1 - e2
    else:
        gap = 0
    return gap <= max_gap


def _extract_keywords(cluster: list[dict]) -> set[str]:
    parts: list[str] = []
    for seg in cluster:
        parts.extend(seg.get("typing") or [])
        if seg.get("cursor_summary"):
            parts.append(seg["cursor_summary"])
    text = " ".join(parts)
    return {
        w.lower()
        for w in text.split()
        if len(w) > 4 and w.lower() not in STOPWORDS
    }


def _sort_segments(segments: list[dict]) -> list[dict]:
    return sorted(segments, key=lambda s: (_time_to_minutes(s.get("start", "")), s.get("end", "")))


def cluster_segments(segments: list[dict]) -> list[list[dict]]:
    """
    Two-pass clustering: temporal+app merge, then keyword overlap merge.

    Returns a list of clusters, each cluster being a list of segment dicts.
    """
    if not segments:
        return []

    cfg = get_config()
    gap_limit: int = int(cfg.get("activity_cluster_gap_minutes", 5))
    keyword_overlap: int = int(cfg.get("activity_cluster_keyword_overlap", 2))

    # ── Pass 1: temporal + app ────────────────────────────────────────────────
    sorted_segs = _sort_segments(segments)
    clusters: list[list[dict]] = []

    for seg in sorted_segs:
        if not clusters:
            clusters.append([seg])
            continue

        last_cluster = clusters[-1]
        prev = last_cluster[-1]
        same_app = _cluster_key(prev) == _cluster_key(seg)
        gap = _gap_minutes(prev.get("end", ""), seg.get("start", ""))

        if same_app and 0 <= gap < gap_limit:
            last_cluster.append(seg)
        else:
            clusters.append([seg])

    # ── Pass 2: keyword overlap (conservative) ────────────────────────────────
    changed = True
    while changed and len(clusters) > 1:
        changed = False
        new_clusters: list[list[dict]] = []
        used = [False] * len(clusters)

        for i in range(len(clusters)):
            if used[i]:
                continue
            current = list(clusters[i])
            used[i] = True
            kw = _extract_keywords(current)

            for j in range(i + 1, len(clusters)):
                if used[j]:
                    continue
                kw_j = _extract_keywords(clusters[j])
                if not kw or not kw_j:
                    continue
                if len(kw & kw_j) < keyword_overlap:
                    continue
                if not _clusters_within_minutes(current, clusters[j], 60):
                    continue
                current.extend(clusters[j])
                current = _sort_segments(current)
                kw = _extract_keywords(current)
                used[j] = True
                changed = True

            new_clusters.append(current)

        clusters = new_clusters

    return clusters


def _cluster_app_label(cluster: list[dict]) -> str:
    domain = cluster[0].get("domain")
    app = cluster[0].get("app", "")
    if domain:
        suffix = f"/{domain}"
        if not app.endswith(suffix):
            base = app.split("/")[0] if "/" in app else app
            return f"{base}/{domain}"
    return app


def _cluster_typing_samples(cluster: list[dict], limit: int = 5) -> list[str]:
    samples: list[str] = []
    for seg in cluster:
        for t in seg.get("typing") or []:
            if t.strip():
                samples.append(_sanitize_for_prompt(t.strip()))
                if len(samples) >= limit:
                    return samples
        if seg.get("cursor_summary"):
            samples.append(_sanitize_for_prompt(seg["cursor_summary"]))
            if len(samples) >= limit:
                return samples
    return samples


def _cluster_metadata(cluster: list[dict]) -> dict:
    sorted_c = _sort_segments(cluster)
    start = sorted_c[0].get("start", "")
    end = sorted_c[-1].get("end", "")
    duration = sum(int(s.get("duration_minutes", 0)) for s in cluster)
    return {
        "time_range": f"{start}–{end}",
        "duration_minutes": duration,
        "app": _cluster_app_label(cluster),
        "typing_samples": _cluster_typing_samples(cluster),
    }


def _build_naming_prompt(clusters: list[list[dict]]) -> str:
    lines: list[str] = [f"Name these {len(clusters)} activity clusters:\n"]
    for i, cluster in enumerate(clusters, 1):
        meta = _cluster_metadata(cluster)
        typing_str = "; ".join(meta["typing_samples"][:5]) or "[no typed content]"
        lines.append(
            f"[{i}] {meta['time_range']} | {meta['app']} | "
            f"{meta['duration_minutes']}min\n"
            f"Typing samples: {typing_str}"
        )
    lines.append(
        "\nReturn JSON array:\n"
        "[\n"
        "  {\n"
        '    "cluster_index": 1,\n'
        '    "suggested_name": "3-6 word action-oriented name",\n'
        '    "description": "one sentence",\n'
        '    "category": "research|communication|coding|admin|learning|planning|other"\n'
        "  }\n"
        "]"
    )
    return "\n".join(lines)


def _naming_max_tokens(n_clusters: int) -> int:
    """Scale output budget so large cluster counts don't truncate mid-JSON."""
    cfg = get_config()
    base = int(cfg.get("activity_naming_max_tokens", 2000))
    return max(800, min(base, 200 * n_clusters + 300))


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    end = -1 if lines[-1].strip() == "```" else len(lines)
    inner = "\n".join(lines[1:end]).strip()
    if inner.lower().startswith("json"):
        inner = inner[4:].strip()
    return inner


def _repair_truncated_array(text: str) -> str:
    """
    Attempt to close a truncated JSON array by cutting at the last complete object.
    """
    start = text.find("[")
    if start < 0:
        raise ValueError("No JSON array start found")
    body = text[start:]

    for cut_at in (body.rfind("},"), body.rfind("}")):
        if cut_at > 0:
            candidate = body[: cut_at + 1] + "\n]"
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue
    raise ValueError("Could not repair truncated JSON array")


def _extract_json_array(text: str) -> list:
    """
    Parse a JSON array from the model response.

    Handles code fences and truncated output (common when max_tokens is tight).
    """
    text = _strip_json_fence(text)
    if not text.startswith("["):
        idx = text.find("[")
        if idx >= 0:
            text = text[idx:]
        else:
            raise ValueError("No JSON array in response")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.debug("JSON parse failed (%s) — attempting repair", exc)
        text = _repair_truncated_array(text)
        data = json.loads(text)

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data)}")
    return data


def _fallback_name(cluster: list[dict]) -> dict:
    meta = _cluster_metadata(cluster)
    app = meta["app"].split("/")[0] if meta["app"] else "Unknown"
    return {
        "suggested_name": f"{app} session",
        "time_range": meta["time_range"],
        "duration_minutes": meta["duration_minutes"],
        "description": "[naming failed]",
        "category": "other",
        "app": meta["app"],
    }


def name_clusters(
    clusters: list[list[dict]],
    trace: DailyPipelineTrace | None = None,
    *,
    unmatched_segments: list[dict] | None = None,
) -> list[dict]:
    """
    Name all clusters in a single LLM call.

    Returns list of dicts matching format_classification_message schema.
    """
    if not clusters:
        return []

    cfg = get_config()
    model: str = cfg.get(
        "anthropic_cursor_presummary_model", "claude-haiku-4-5-20251001"
    )
    metas = [_cluster_metadata(c) for c in clusters]
    max_tokens = _naming_max_tokens(len(clusters))
    user_prompt = _build_naming_prompt(clusters)

    def _call_and_parse(extra_user_note: str = "") -> tuple[list[dict], str]:
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        content = user_prompt + extra_user_note
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        in_tok = msg.usage.input_tokens
        out_tok = msg.usage.output_tokens
        try:
            from utils.cost_logger import log_api_call
            log_api_call(model, in_tok, out_tok, "activity_naming")
        except Exception:
            pass
        raw = msg.content[0].text
        named = _extract_json_array(raw)
        log.info(
            "Named %d cluster(s): %d in + %d out tokens (max_out=%d)",
            len(clusters), in_tok, out_tok, max_tokens,
        )
        return named, raw

    named: list[dict] | None = None
    raw_response = ""
    try:
        named, raw_response = _call_and_parse()
    except Exception as exc:
        log.warning(
            "activity_naming parse failed (%s) — retrying with stricter JSON instruction",
            exc,
        )
        try:
            named, raw_response = _call_and_parse(
                "\n\nIMPORTANT: Return a COMPLETE valid JSON array. "
                "One object per cluster. No trailing commas. No markdown fences."
            )
        except Exception as exc2:
            log.warning("activity_naming LLM failed (%s) — using fallbacks", exc2)
            if trace:
                trace.add_stage(
                    "5. Activity namer (Haiku)",
                    input_data={"cluster_count": len(clusters)},
                    prompt=user_prompt,
                    system=_SYSTEM_PROMPT,
                    notes=f"LLM failed: {exc2}",
                    output_data=[_fallback_name(c) for c in clusters],
                )
            return [_fallback_name(c) for c in clusters]

    results: list[dict] = []
    for i, cluster in enumerate(clusters):
        meta = metas[i]
        item = named[i] if named and i < len(named) else {}
        if not isinstance(item, dict) or not item.get("suggested_name"):
            log.debug("Cluster %d: missing name in LLM output — fallback", i + 1)
            results.append(_fallback_name(cluster))
            continue
        results.append({
            "suggested_name": str(item["suggested_name"]).strip(),
            "time_range": meta["time_range"],
            "duration_minutes": meta["duration_minutes"],
            "description": str(item.get("description", "")).strip() or "[no description]",
            "category": str(item.get("category", "other")).strip() or "other",
            "app": meta["app"],
        })

    if named and len(named) < len(clusters):
        log.warning(
            "activity_naming returned %d/%d clusters — filled rest with fallbacks",
            len(named), len(clusters),
        )
        while len(results) < len(clusters):
            results.append(_fallback_name(clusters[len(results)]))

    if trace:
        cluster_summaries = [
            {
                "index": i + 1,
                "time_range": _cluster_metadata(c)["time_range"],
                "app": _cluster_metadata(c)["app"],
                "segments": len(c),
                "duration_minutes": _cluster_metadata(c)["duration_minutes"],
            }
            for i, c in enumerate(clusters)
        ]
        trace.add_stage(
            "5. Activity namer (Haiku)",
            input_data={
                "unmatched_segments": unmatched_segments,
                "clusters": cluster_summaries,
            },
            system=_SYSTEM_PROMPT,
            prompt=user_prompt,
            llm_raw=raw_response,
            output_data=results,
        )

    return results


def name_unclassified(
    unmatched_segments: list[dict],
    trace: DailyPipelineTrace | None = None,
) -> list[dict]:
    """
    Top-level entry: cluster unmatched segments and name them.

    Drops clusters with total duration < 3 min.
    """
    if not unmatched_segments:
        if trace:
            trace.add_stage(
                "5. Activity namer (Haiku)",
                notes="No unmatched segments — skipped.",
                output_data=[],
            )
        return []

    clusters = cluster_segments(unmatched_segments)
    clusters = [
        c for c in clusters
        if sum(int(s.get("duration_minutes", 0)) for s in c) >= 3
    ]
    if not clusters:
        if trace:
            trace.add_stage(
                "5. Activity namer (Haiku)",
                input_data={"unmatched_segments": unmatched_segments},
                notes="All clusters below 3 min — skipped naming.",
                output_data=[],
            )
        return []

    return name_clusters(
        clusters, trace=trace, unmatched_segments=unmatched_segments,
    )


# ── Test fixture ──────────────────────────────────────────────────────────────

def _test_fixture() -> list[dict]:
    """6 segments → expect 4 clusters after pass 1."""
    return [
        {
            "start": "09:00", "end": "09:18", "duration_minutes": 18,
            "app": "Google Chrome", "domain": "claude.ai",
            "typing": ["research procurement market size for B2B SaaS"],
            "cursor_summary": None,
        },
        {
            "start": "09:20", "end": "09:35", "duration_minutes": 15,
            "app": "Google Chrome", "domain": "claude.ai",
            "typing": ["procurement market size TAM analysis enterprise"],
            "cursor_summary": None,
        },
        {
            "start": "09:37", "end": "09:50", "duration_minutes": 13,
            "app": "Google Chrome", "domain": "claude.ai",
            "typing": ["procurement market sizing methodology"],
            "cursor_summary": None,
        },
        {
            "start": "10:30", "end": "10:38", "duration_minutes": 8,
            "app": "Gmail", "domain": None,
            "typing": [], "cursor_summary": None,
        },
        {
            "start": "11:00", "end": "11:15", "duration_minutes": 15,
            "app": "LinkedIn", "domain": None,
            "typing": [], "cursor_summary": None,
        },
        {
            "start": "14:00", "end": "14:12", "duration_minutes": 12,
            "app": "Google Chrome", "domain": "ycombinator.com",
            "typing": ["reading YC startup advice posts"],
            "cursor_summary": None,
        },
    ]


def _run_test() -> None:
    print("=== Empty input ===")
    assert name_unclassified([]) == []
    print("✓ empty input returns []")

    segments = _test_fixture()
    clusters = cluster_segments(segments)
    print(f"\n=== Clustering ({len(segments)} segments → {len(clusters)} clusters) ===")
    for i, cluster in enumerate(clusters, 1):
        meta = _cluster_metadata(cluster)
        print(
            f"  [{i}] {meta['time_range']} | {meta['app']} | "
            f"{meta['duration_minutes']}min | {len(cluster)} segment(s)"
        )

    assert len(clusters) == 4, f"Expected 4 clusters, got {len(clusters)}"
    print("\n✓ Pass 1 produced 4 clusters")

    # First cluster should merge all 3 claude.ai segments
    claude_cluster = max(clusters, key=len)
    assert len(claude_cluster) == 3, (
        f"Expected 3 claude.ai segments merged, got {len(claude_cluster)}"
    )
    print("✓ 3 claude.ai segments merged into 1 cluster")

    print("\n=== Naming (LLM) ===")
    names = name_clusters(clusters)
    for i, n in enumerate(names, 1):
        print(
            f"  [{i}] {n['suggested_name']} — {n['time_range']} "
            f"({n['duration_minutes']}m) [{n['category']}]"
        )
        print(f"      {n['description']}")

    print("\n=== Full pipeline ===")
    # Simulate classifier leftovers (all 6 as unmatched)
    full = name_unclassified(segments)
    print(f"name_unclassified → {len(full)} named activit(ies)")
    assert len(full) == 4
    print("\n✓ All tests passed")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Unclassified activity namer")
    parser.add_argument(
        "--test", action="store_true",
        help="Run built-in fixture (clustering + naming)",
    )
    args = parser.parse_args()
    if args.test:
        _run_test()
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
