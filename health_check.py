#!/usr/bin/env python3
"""
Daily Agent — pre-flight health check.

Run this before the first real pipeline execution to verify all
external dependencies are configured and reachable.

    python health_check.py

Each check prints ✅ on success or ❌ on failure with an error hint.

Exit code: 0 if every check passes, 1 if any check fails.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

_REQUIRED_CONFIG_KEYS = [
    "anthropic_api_key",
    "telegram_bot_token",
    "telegram_chat_id",
    "notion_api_key",
    "notion_meetings_db_id",
    "notion_context_page_id",
    "typing_log_dir",
    "plans_dir",
]

_RESULTS: list[tuple[str, bool, str]] = []  # (label, passed, detail)


def _check(label: str, fn) -> bool:
    """Run *fn*; record and print result. Returns True on success."""
    try:
        detail = fn() or ""
        _RESULTS.append((label, True, detail))
        suffix = f"  — {detail}" if detail else ""
        print(f"  ✅  {label}{suffix}")
        return True
    except Exception as exc:
        _RESULTS.append((label, False, str(exc)))
        print(f"  ❌  {label}  — {exc}")
        return False


# ── Individual checks ─────────────────────────────────────────────────────────

def check_config() -> str:
    """config.yaml is readable and all required keys are present."""
    from config_loader import get_config
    cfg = get_config()

    missing = [k for k in _REQUIRED_CONFIG_KEYS if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"Missing required keys: {', '.join(missing)}")

    # Sanity-check key formats
    api_key = cfg.get("anthropic_api_key", "")
    if not api_key.startswith("sk-ant-"):
        raise RuntimeError(
            f"anthropic_api_key looks wrong (got: {api_key[:12]}…)"
        )

    return f"{len(cfg)} keys loaded"


def check_typing_log_dir() -> str:
    """Typing log directory (~/.typing-log) exists."""
    from config_loader import get_config
    cfg = get_config()
    d = pathlib.Path(cfg.get("typing_log_dir", "~/.typing-log")).expanduser()
    if not d.exists():
        raise RuntimeError(
            f"{d} does not exist. "
            "Is the typing daemon running? See daemon/typing-daemon."
        )
    files = list(d.glob("*.json"))
    return f"{d}  ({len(files)} log file(s))"


def check_plans_dir() -> str:
    """Plans dir (~/.daily-agent/plans) is writable."""
    from config_loader import get_config
    cfg = get_config()
    d = pathlib.Path(cfg.get("plans_dir", "~/.daily-agent/plans")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    # Write + delete a sentinel file to verify write access
    sentinel = d / ".health_check_sentinel"
    sentinel.write_text("ok")
    sentinel.unlink()
    return str(d)


def check_activitywatch() -> str:
    """ActivityWatch REST API is reachable on localhost:5600."""
    import requests
    from config_loader import get_config
    cfg = get_config()
    host = cfg.get("activitywatch_host", "http://localhost:5600")
    url  = f"{host}/api/0/info"
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    version = data.get("version", "?")
    return f"{host}  (AW {version})"


def check_notion() -> str:
    """Notion API is reachable and the integration has access."""
    from config_loader import get_config
    cfg = get_config()
    import requests
    headers = {
        "Authorization": f"Bearer {cfg['notion_api_key']}",
        "Notion-Version": "2022-06-28",
    }
    # /v1/users/me is the lightest endpoint
    resp = requests.get("https://api.notion.com/v1/users/me",
                        headers=headers, timeout=10)
    if resp.status_code == 401:
        raise RuntimeError("Invalid Notion API key (401 Unauthorized)")
    resp.raise_for_status()
    name = resp.json().get("name") or resp.json().get("bot", {}).get("owner", {}).get("user", {}).get("name", "?")
    return f"authenticated as '{name}'"


def check_notion_pages() -> str:
    """Notion meetings DB, context page, Tasks DB, and Projects DB are accessible."""
    from config_loader import get_config
    cfg = get_config()
    import requests

    headers = {
        "Authorization": f"Bearer {cfg['notion_api_key']}",
        "Notion-Version": "2022-06-28",
    }

    _CHECKS = [
        ("notion_meetings_db_id",   "databases",  "meetings DB"),
        ("notion_context_page_id",  "pages",      "context page"),
        ("notion_tasks_db_id",      "databases",  "Tasks DB"),
        ("notion_projects_db_id",   "databases",  "Projects DB"),
    ]

    errors: list[str] = []
    ok_count = 0

    for cfg_key, endpoint, label in _CHECKS:
        obj_id = cfg.get(cfg_key, "")
        if not obj_id:
            continue
        r = requests.get(
            f"https://api.notion.com/v1/{endpoint}/{obj_id}",
            headers=headers, timeout=10,
        )
        if r.status_code == 404:
            errors.append(
                f"{label} {obj_id[:8]}… not found (404) — "
                f"open it in Notion → ··· → Connections → add integration"
            )
        elif r.status_code == 403:
            errors.append(f"{label} {obj_id[:8]}… not shared with integration (403)")
        elif not r.ok:
            errors.append(f"{label} error {r.status_code}")
        else:
            ok_count += 1

    if errors:
        raise RuntimeError("; ".join(errors))

    return f"{ok_count}/4 Notion objects accessible"


def check_telegram() -> str:
    """Telegram bot token is valid and the bot is reachable."""
    import requests
    from config_loader import get_config
    cfg = get_config()
    token   = cfg.get("telegram_bot_token", "")
    chat_id = cfg.get("telegram_chat_id", "")
    if not token:
        raise RuntimeError("telegram_bot_token is empty")

    url  = f"https://api.telegram.org/bot{token}/getMe"
    resp = requests.get(url, timeout=10)
    if resp.status_code == 401:
        raise RuntimeError("Invalid bot token (401 Unauthorized)")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getMe returned ok=false: {data}")

    bot_name = data["result"].get("username", "?")
    return f"@{bot_name}  (chat_id={chat_id})"


def check_typing_daemon() -> str:
    """Typing capture daemon (com.user.typing-capture) is running and writing logs."""
    import datetime
    import json
    import subprocess

    label = "com.user.typing-capture"

    # ── 1. Is the launchd job loaded and running? ─────────────────────────────
    result = subprocess.run(
        ["launchctl", "list", label],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"launchd job '{label}' is not loaded. "
            "Run: launchctl load ~/Library/LaunchAgents/com.user.typing-capture.plist"
        )

    import re
    # launchctl list outputs a NeXTStep text-plist (key = value; pairs)
    pid_match  = re.search(r'"PID"\s*=\s*(\d+)', result.stdout)
    exit_match = re.search(r'"LastExitStatus"\s*=\s*(\d+)', result.stdout)

    pid       = int(pid_match.group(1))  if pid_match  else None
    last_exit = int(exit_match.group(1)) if exit_match else 0

    if pid is None:
        hint = ""
        if last_exit != 0:
            stderr_log = pathlib.Path.home() / "typing-logs" / "capture-stderr.log"
            if stderr_log.exists():
                tail = stderr_log.read_text(errors="replace").strip().splitlines()
                last_lines = " | ".join(tail[-3:]) if tail else ""
                hint = f" Last error: {last_lines}" if last_lines else ""
        raise RuntimeError(
            f"Daemon loaded but not running (LastExitStatus={last_exit}).{hint} "
            "Run: launchctl start com.user.typing-capture"
        )

    # ── 2. Did it write a log today (or at least this week)? ──────────────────
    from config_loader import get_config
    cfg = get_config()
    log_dir = pathlib.Path(cfg.get("typing_log_dir", "~/.typing-log")).expanduser()

    today = datetime.date.today()
    today_file = log_dir / f"{today}.json"

    if not today_file.exists():
        # Check most recent file to give a useful hint
        existing = sorted(log_dir.glob("*.json"))
        last = existing[-1].stem if existing else "none"
        raise RuntimeError(
            f"No log file for today ({today_file.name}). "
            f"Latest log: {last}. Daemon is running (PID={pid}) but not writing — "
            "may lack Accessibility permission. Check System Settings → Privacy & Security → Accessibility."
        )

    # ── 3. Are there entries in today's log? ──────────────────────────────────
    try:
        entries = json.loads(today_file.read_text())
    except Exception as exc:
        raise RuntimeError(f"Could not parse {today_file.name}: {exc}")

    count = len(entries) if isinstance(entries, list) else 0
    if count == 0:
        raise RuntimeError(
            f"{today_file.name} exists but has 0 entries. "
            "Daemon may lack Accessibility permission — "
            "check System Settings → Privacy & Security → Accessibility."
        )

    # ── 4. Check recency: last entry should be within 6 hours ─────────────────
    warnings = []
    if isinstance(entries, list) and entries:
        last_entry = entries[-1]
        ts_str = last_entry.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                age_hours = (now_utc - ts).total_seconds() / 3600
                if age_hours > 6:
                    warnings.append(f"last entry was {age_hours:.1f}h ago — is the daemon stuck?")
            except ValueError:
                pass

    detail = f"PID={pid}  {count} entries today"
    if warnings:
        detail += f"  ⚠ {warnings[0]}"
    return detail


def check_anthropic_key() -> str:
    """Anthropic API key format is valid (does not make an API call)."""
    from config_loader import get_config
    cfg = get_config()
    key = cfg.get("anthropic_api_key", "")
    if not key:
        raise RuntimeError("anthropic_api_key is empty")
    if not key.startswith("sk-ant-"):
        raise RuntimeError(
            f"Key doesn't look like a valid Anthropic key "
            f"(expected 'sk-ant-…', got '{key[:12]}…')"
        )
    model = cfg.get("anthropic_model", "")
    return f"key OK  (model: {model})"


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Daily Agent health check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--skip-notion", action="store_true",
        help="Skip Notion connectivity checks",
    )
    parser.add_argument(
        "--skip-activitywatch", action="store_true",
        help="Skip ActivityWatch check (if AW isn't running)",
    )
    parser.add_argument(
        "--skip-telegram", action="store_true",
        help="Skip Telegram check",
    )
    args = parser.parse_args()

    print("\nDaily Agent — health check\n")
    t0 = time.monotonic()

    checks = [
        ("Config (config.yaml)",            check_config),
        ("Typing log dir",                  check_typing_log_dir),
        ("Typing daemon (running)",         check_typing_daemon),
        ("Plans dir (writable)",            check_plans_dir),
        ("Anthropic API key",               check_anthropic_key),
    ]
    if not args.skip_activitywatch:
        checks.append(("ActivityWatch",     check_activitywatch))
    if not args.skip_notion:
        checks.append(("Notion API",        check_notion))
        checks.append(("Notion pages/DBs",  check_notion_pages))
    if not args.skip_telegram:
        checks.append(("Telegram bot",      check_telegram))

    for label, fn in checks:
        _check(label, fn)

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = sum(1 for _, ok, _ in _RESULTS if not ok)
    elapsed = time.monotonic() - t0

    print(f"\n{'─' * 50}")
    if failed == 0:
        print(f"  ✅  All {passed} checks passed  ({elapsed:.1f}s)")
    else:
        print(f"  {passed} passed  /  {failed} failed  ({elapsed:.1f}s)")
        print()
        print("  Failed checks:")
        for label, ok, detail in _RESULTS:
            if not ok:
                print(f"    ✗  {label}: {detail}")
        print()
        print("  Fix the issues above, then re-run `python health_check.py`")
    print()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
