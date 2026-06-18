"""
Cursor IDE collector.

Extracts user-turn prompts from Cursor's SQLite workspace storage.

Real storage layout (confirmed by probing, different from CLAUDE.md docs):
  ~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/state.vscdb
    ItemTable key "aiService.generations"
      → rolling buffer (≤50) of {unixMs, generationUUID, type, textDescription}
        textDescription = the user's raw prompt text
    ItemTable key "composer.composerData"
      → {allComposers: [...]} — pre-migration workspaces only
        post-migration: allComposers is empty; sessions live in global storage

  ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
    ItemTable key "composer.composerHeaders"
      → {allComposers: [...]} — all sessions with workspaceIdentifier.id → hash

  <ws_dir>/workspace.json  → {folder: "file:///path/to/project"} — project path

Fallback paths also tried: ~/.cursor/User/workspaceStorage/ (older Cursor installs)
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sqlite3
import sys
from typing import Any

import pytz

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("collect_cursor")

# ── path discovery ─────────────────────────────────────────────────────────────

def _workspace_roots() -> list[pathlib.Path]:
    """Return candidate workspaceStorage paths, most likely first."""
    home = pathlib.Path.home()
    return [
        home / "Library/Application Support/Cursor/User/workspaceStorage",
        home / ".cursor/User/workspaceStorage",
    ]


def _global_db_candidates() -> list[pathlib.Path]:
    home = pathlib.Path.home()
    return [
        home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
        home / ".cursor/User/globalStorage/state.vscdb",
    ]


def _find_workspace_root() -> pathlib.Path | None:
    for p in _workspace_roots():
        if p.is_dir():
            return p
    return None


def _find_global_db() -> pathlib.Path | None:
    for p in _global_db_candidates():
        if p.exists():
            return p
    return None


# ── SQLite helpers ─────────────────────────────────────────────────────────────

def _read_item(db: pathlib.Path, key: str) -> Any | None:
    """Read and JSON-parse one ItemTable row; return None on any error."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception as exc:
        log.debug("read_item(%s, %r) failed: %s", db.name, key, exc)
        return None


# ── workspace metadata ─────────────────────────────────────────────────────────

def _workspace_name(ws_dir: pathlib.Path) -> str:
    """Return the project folder name for a workspace hash directory."""
    wj = ws_dir / "workspace.json"
    if wj.exists():
        try:
            folder = json.loads(wj.read_text()).get("folder", "")
            # "file:///Users/.../project-name" → "project-name"
            raw = folder.replace("file://", "").strip("/")
            name = pathlib.PurePosixPath(raw).name
            if name:
                return name
        except Exception:
            pass
    return ws_dir.name  # fallback: hash


# ── date conversion ────────────────────────────────────────────────────────────

def _ms_to_date(ms: int, tz: datetime.tzinfo) -> datetime.date:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).astimezone(tz).date()


def _ms_to_iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()


# ── session metadata loaders ──────────────────────────────────────────────────

def _local_composers(db: pathlib.Path) -> list[dict]:
    """
    Per-workspace composers (pre-migration: allComposers is non-empty).
    """
    data = _read_item(db, "composer.composerData")
    if not data:
        return []
    return data.get("allComposers") or []


def _global_composers_for_workspace(ws_hash: str) -> list[dict]:
    """
    Post-migration: composers live in global storage keyed by workspaceIdentifier.id.
    """
    global_db = _find_global_db()
    if not global_db:
        return []
    data = _read_item(global_db, "composer.composerHeaders")
    if not data:
        return []
    return [
        c for c in data.get("allComposers", [])
        if (c.get("workspaceIdentifier") or {}).get("id") == ws_hash
    ]


# ── session assignment ─────────────────────────────────────────────────────────

def _assign_generations_to_sessions(
    gens: list[dict],
    composers: list[dict],
) -> dict[str, list[dict]]:
    """
    Assign each generation to a composerId.

    Strategy: each generation belongs to the composer whose createdAt is the
    latest one that is still ≤ the generation's unixMs.  This handles sequential
    sessions correctly.  For parallel sub-composers, it assigns to the most
    recently started one — good enough for daily summaries.

    Returns {composerId: [sorted generations]}.
    """
    if not composers:
        return {"__unassigned__": sorted(gens, key=lambda g: g.get("unixMs", 0))}

    composers_by_time = sorted(composers, key=lambda c: c.get("createdAt", 0))
    buckets: dict[str, list[dict]] = {}

    for g in sorted(gens, key=lambda x: x.get("unixMs", 0)):
        gts = g.get("unixMs", 0)

        # Walk backwards from the end to find the latest composer that started ≤ gts
        assigned_id = "__unassigned__"
        for c in reversed(composers_by_time):
            if c.get("createdAt", 0) <= gts:
                assigned_id = c.get("composerId", "__unassigned__")
                break

        buckets.setdefault(assigned_id, []).append(g)

    return buckets


def _composer_by_id(composers: list[dict], cid: str) -> dict:
    for c in composers:
        if c.get("composerId") == cid:
            return c
    return {}


# ── per-workspace extraction ──────────────────────────────────────────────────

def _extract_workspace(
    ws_dir: pathlib.Path,
    target: datetime.date,
    tz: datetime.tzinfo,
) -> list[dict]:
    """
    Return session dicts for *target* from one workspace directory.
    Tries state.vscdb first, then backup.db.
    """
    results: list[dict] = []
    ws_name = _workspace_name(ws_dir)
    ws_hash = ws_dir.name

    for db_name in ("state.vscdb", "backup.db"):
        db = ws_dir / db_name
        if not db.exists():
            continue

        # ── user turns ────────────────────────────────────────────────────────
        raw_gens = _read_item(db, "aiService.generations") or []
        # Also try the CLAUDE.md-documented key as a fallback (older Cursor versions)
        if not raw_gens:
            chat_data = _read_item(db, "workbench.panel.aichat.view.aichat.chatdata")
            if chat_data:
                raw_gens = _extract_gens_from_chat_data(chat_data)

        if not raw_gens:
            continue

        # Filter to target date
        day_gens = [
            g for g in raw_gens
            if _ms_to_date(g.get("unixMs", 0), tz) == target
        ]
        if not day_gens:
            continue

        log.debug(
            "ws=%s db=%s: %d/%d generations match %s",
            ws_name, db_name, len(day_gens), len(raw_gens), target,
        )

        # ── session metadata ──────────────────────────────────────────────────
        composers = _local_composers(db)
        if not composers:
            # post-migration: look in global storage
            composers = _global_composers_for_workspace(ws_hash)

        assignments = _assign_generations_to_sessions(day_gens, composers)

        for cid, session_gens in assignments.items():
            if not session_gens:
                continue

            meta = _composer_by_id(composers, cid)
            session_name = meta.get("name") or cid  # e.g. "GPS data transmission…"

            first_ts = min(g.get("unixMs", 0) for g in session_gens)
            user_turns = [
                g["textDescription"].strip()
                for g in sorted(session_gens, key=lambda x: x.get("unixMs", 0))
                if g.get("textDescription", "").strip()
            ]

            if not user_turns:
                continue

            results.append({
                "session_id": cid if cid != "__unassigned__" else f"{ws_hash[:8]}-{target}",
                "workspace": ws_name,
                "session_name": session_name,
                "started_at": _ms_to_iso(first_ts),
                "user_turns": user_turns,
                "turn_count": len(user_turns),
            })

        # Found data in this db — no need to try the other one for this workspace
        break

    return results


def _extract_gens_from_chat_data(chat_data: Any) -> list[dict]:
    """
    Fallback: parse older `workbench.panel.aichat.view.aichat.chatdata` format.
    Returns a list of synthetic generation dicts.
    """
    gens: list[dict] = []
    try:
        tabs = chat_data if isinstance(chat_data, list) else chat_data.get("tabs", [])
        for tab in tabs:
            for msg in tab.get("messages", []):
                if msg.get("role") != "user":
                    continue
                text = msg.get("content") or msg.get("text") or ""
                ts = msg.get("timestamp") or msg.get("createdAt") or 0
                if not text.strip():
                    continue
                gens.append({
                    "unixMs": int(ts) if ts else 0,
                    "generationUUID": msg.get("id", ""),
                    "type": "chat",
                    "textDescription": text,
                })
    except Exception as exc:
        log.debug("_extract_gens_from_chat_data failed: %s", exc)
    return gens


# ── public API ─────────────────────────────────────────────────────────────────

def get_date(d: datetime.date) -> list[dict]:
    """Return all Cursor sessions with user turns on date *d*."""
    cfg = get_config()
    try:
        tz = pytz.timezone(cfg.get("timezone", "UTC"))
    except Exception:
        tz = datetime.timezone.utc

    root = _find_workspace_root()
    if root is None:
        log.warning(
            "Cursor workspaceStorage not found at: %s",
            ", ".join(str(p) for p in _workspace_roots()),
        )
        return []

    all_sessions: list[dict] = []
    for ws_dir in root.iterdir():
        if not ws_dir.is_dir():
            continue
        try:
            sessions = _extract_workspace(ws_dir, d, tz)
            all_sessions.extend(sessions)
        except Exception as exc:
            log.warning("Error processing workspace %s: %s", ws_dir.name, exc)

    # Sort by started_at ascending
    all_sessions.sort(key=lambda s: s.get("started_at", ""))
    log.debug("get_date(%s): %d session(s) found", d, len(all_sessions))
    return all_sessions


def get_today() -> list[dict]:
    return get_date(datetime.date.today())


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Cursor session collector")
    parser.add_argument(
        "--date", default=str(datetime.date.today()), help="YYYY-MM-DD (default: today)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pretty-print session table and first 80 chars of each turn",
    )
    args = parser.parse_args()

    try:
        d = datetime.date.fromisoformat(args.date)
    except ValueError:
        print(f"Invalid date: {args.date!r}", file=sys.stderr)
        sys.exit(1)

    sessions = get_date(d)

    if not sessions:
        print(f"No Cursor sessions found for {d}.")
        return

    if args.dry_run:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        console = Console()
        console.print(
            f"\n[bold]Cursor sessions — {d}[/]  "
            f"({len(sessions)} session(s), "
            f"{sum(s['turn_count'] for s in sessions)} total turns)\n"
        )

        for s in sessions:
            t = Table(
                title=f"[cyan]{s['workspace']}[/]  ·  [dim]{s['session_id'][:16]}…[/]",
                show_lines=False,
                expand=False,
            )
            t.add_column("#", style="dim", width=3)
            t.add_column("Turn (first 80 chars)", no_wrap=False)
            t.add_column("chars", justify="right", style="dim")

            for i, turn in enumerate(s["user_turns"], 1):
                snippet = turn[:80].replace("\n", " ")
                if len(turn) > 80:
                    snippet += "…"
                t.add_row(str(i), snippet, str(len(turn)))

            console.print(t)
            console.print(
                f"  [dim]started_at:[/] {s['started_at']}  "
                f"[dim]turns:[/] {s['turn_count']}\n"
            )
    else:
        import json as _json
        # Strip session_name from output (internal field not in spec)
        out = [{k: v for k, v in s.items() if k != "session_name"} for s in sessions]
        print(_json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
