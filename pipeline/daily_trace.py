"""
Daily pipeline debug trace — writes summarization_prompts/YYYY-MM-DD.md.

Captures truncated inputs/outputs for each pipeline stage on every run_daily execution.

Public API
----------
DailyPipelineTrace.for_run(date, dry_run=False) -> DailyPipelineTrace | None
trace.add_stage(title, input=..., output=..., prompt=..., system=..., notes=...)
trace.set_run_meta(exit_code=..., elapsed_s=..., error=...)
trace.write()
"""

from __future__ import annotations

import copy
import datetime
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("daily_trace")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _trace_limits() -> dict[str, int]:
    cfg = get_config()
    return {
        "max_list": int(cfg.get("debug_trace_max_list_items", 50)),
        "max_aw_events": int(cfg.get("debug_trace_max_aw_events", 100)),
        "max_chars": int(cfg.get("debug_trace_max_section_chars", 30000)),
        "max_cursor_turns": 5,
    }


def trace_output_dir() -> pathlib.Path:
    cfg = get_config()
    rel = cfg.get("summarization_prompts_dir", "summarization_prompts")
    p = pathlib.Path(rel)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    else:
        p = p.expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def trace_enabled() -> bool:
    return bool(get_config().get("debug_trace_enabled", True))


class DailyPipelineTrace:
    """Accumulates markdown sections for one pipeline run."""

    def __init__(self, date: datetime.date, *, dry_run: bool = False) -> None:
        self.date = date
        self.dry_run = dry_run
        self._sections: list[str] = []
        self._started = datetime.datetime.now(tz=datetime.timezone.utc)
        self._exit_code: int | None = None
        self._elapsed_s: float | None = None
        self._error: str | None = None
        self._limits = _trace_limits()

    @classmethod
    def for_run(
        cls,
        date: datetime.date,
        *,
        dry_run: bool = False,
        enabled: bool | None = None,
    ) -> DailyPipelineTrace | None:
        if enabled is False:
            return None
        if enabled is None and not trace_enabled():
            return None
        return cls(date, dry_run=dry_run)

    def set_run_meta(
        self,
        *,
        exit_code: int | None = None,
        elapsed_s: float | None = None,
        error: str | None = None,
    ) -> None:
        if exit_code is not None:
            self._exit_code = exit_code
        if elapsed_s is not None:
            self._elapsed_s = elapsed_s
        if error is not None:
            self._error = error

    def add_stage(
        self,
        title: str,
        *,
        input_data: Any = None,
        output_data: Any = None,
        prompt: str | None = None,
        system: str | None = None,
        llm_raw: Any = None,
        notes: str | None = None,
    ) -> None:
        """Append one markdown section for a pipeline stage."""
        parts: list[str] = [f"## {title}", ""]
        if notes:
            parts.extend([notes, ""])
        if input_data is not None:
            parts.append("### Input")
            parts.append(self._format_value(input_data))
            parts.append("")
        if system:
            parts.append("### System prompt")
            parts.append(self._fence_text(system))
            parts.append("")
        if prompt:
            parts.append("### User prompt")
            parts.append(self._fence_text(prompt))
            parts.append("")
        if llm_raw is not None:
            parts.append("### Raw LLM response")
            parts.append(self._format_value(llm_raw))
            parts.append("")
        if output_data is not None:
            parts.append("### Output")
            parts.append(self._format_value(output_data))
            parts.append("")
        self._sections.append("\n".join(parts).rstrip())

    def add_collect(self, data: dict) -> None:
        """Stage 1 — raw collector outputs with sensible truncation."""
        lim = self._limits
        typing = data.get("typing") or []
        aw = data.get("activitywatch") or {}
        cursor = data.get("cursor") or []
        meetings = data.get("meetings") or []
        context = data.get("context") or {}

        aw_out: dict = {}
        if isinstance(aw, dict):
            aw_out = {k: v for k, v in aw.items() if k != "raw_events"}
            raw = aw.get("raw_events") or []
            aw_out["raw_events_count"] = len(raw)
            aw_out["raw_events_sample"] = raw[: lim["max_aw_events"]]
            if len(raw) > lim["max_aw_events"]:
                aw_out["_truncated"] = f"... and {len(raw) - lim['max_aw_events']} more events"

        ctx_out = copy.deepcopy(context) if isinstance(context, dict) else context
        if isinstance(ctx_out, dict) and ctx_out.get("general"):
            g = str(ctx_out["general"])
            if len(g) > 2000:
                ctx_out["general"] = g[:2000] + "\n[... truncated ...]"

        payload = {
            "typing": self._truncate_list(typing, lim["max_list"]),
            "typing_count": len(typing),
            "activitywatch": aw_out,
            "cursor_sessions": self._truncate_cursor_sessions(cursor, lim["max_list"]),
            "cursor_session_count": len(cursor),
            "meetings": self._truncate_list(meetings, lim["max_list"]),
            "meetings_count": len(meetings),
            "context": ctx_out,
        }
        self.add_stage("1. Collect — raw inputs", output_data=payload)

    def add_final_result(self, result: dict) -> None:
        self.add_stage("7. Final pipeline result", output_data=result)

    def add_telegram_preview(self, message: str) -> None:
        self.add_stage(
            "8. Telegram message preview",
            output_data=message,
        )

    def write(self) -> pathlib.Path | None:
        """Write summarization_prompts/YYYY-MM-DD.md."""
        out_dir = trace_output_dir()
        path = out_dir / f"{self.date.isoformat()}.md"

        meta = (
            f"**Run:** {self._started.isoformat(timespec='seconds')} "
            f"| dry_run={self.dry_run}"
        )
        if self._exit_code is not None:
            meta += f" | exit={self._exit_code}"
        if self._elapsed_s is not None:
            meta += f" | elapsed={self._elapsed_s:.1f}s"
        meta += "**"

        body_parts = [
            f"# Daily Pipeline Trace — {self.date.isoformat()}",
            "",
            meta,
            "",
            "---",
            "",
            *self._sections,
        ]

        if self._error:
            body_parts.extend([
                "",
                "## Error",
                "",
                self._fence_text(self._error),
            ])

        text = "\n".join(body_parts).strip() + "\n"
        path.write_text(text, encoding="utf-8")
        log.info("Debug trace written: %s (%d chars)", path, len(text))
        return path

    def _fence_text(self, text: str) -> str:
        return f"```text\n{text}\n```"

    def _format_value(self, value: Any) -> str:
        if isinstance(value, str):
            return self._truncate_chars(self._fence_text(value))
        try:
            serialized = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = repr(value)
        return self._truncate_chars(f"```json\n{serialized}\n```")

    def _truncate_chars(self, text: str) -> str:
        max_c = self._limits["max_chars"]
        if len(text) <= max_c:
            return text
        return text[:max_c] + "\n```\n[... section truncated ...]"

    def _truncate_list(self, items: list, max_items: int) -> list:
        if len(items) <= max_items:
            return items
        out = items[:max_items]
        return out + [{"_note": f"... and {len(items) - max_items} more items"}]

    def _truncate_cursor_sessions(self, sessions: list, max_sessions: int) -> list:
        lim = self._limits
        out: list[dict] = []
        for s in (sessions or [])[:max_sessions]:
            if not isinstance(s, dict):
                out.append(s)
                continue
            copy_s = dict(s)
            turns = copy_s.get("user_turns") or []
            if len(turns) > lim["max_cursor_turns"]:
                copy_s["user_turns"] = turns[: lim["max_cursor_turns"]]
                copy_s["_turns_truncated"] = f"... and {len(turns) - lim['max_cursor_turns']} more"
            for i, t in enumerate(copy_s.get("user_turns") or []):
                if isinstance(t, str) and len(t) > 500:
                    copy_s["user_turns"][i] = t[:500] + "..."
            out.append(copy_s)
        if len(sessions or []) > max_sessions:
            out.append({"_note": f"... and {len(sessions) - max_sessions} more sessions"})
        return out
