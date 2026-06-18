"""
ActivityWatch collector.

Queries the local AW REST API for app-time, domain-time, and active/idle totals
for a given day.  Returns a structured dict; never raises on connection failure.
"""

from __future__ import annotations

import argparse
import datetime
import math
import pathlib
import sys
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.logger import get_logger

log = get_logger("collect_activitywatch")

# ── app → category mapping ─────────────────────────────────────────────────────
_CATEGORIES: dict[str, str] = {
    "Google Chrome": "browser", "Chromium": "browser", "Safari": "browser",
    "Firefox": "browser", "Arc": "browser", "Brave Browser": "browser",
    "Opera": "browser",
    "Cursor": "dev", "Code": "dev", "Visual Studio Code": "dev",
    "Xcode": "dev", "Terminal": "dev", "iTerm2": "dev", "iTerm": "dev",
    "Warp": "dev", "RubyMine": "dev", "PyCharm": "dev", "IntelliJ IDEA": "dev",
    "Claude": "ai",
    "Notion": "writing", "Microsoft Word": "writing", "Word": "writing",
    "Pages": "writing", "Notes": "writing", "Bear": "writing",
    "Obsidian": "writing", "Microsoft PowerPoint": "writing",
    "Slack": "comms", "Messages": "comms", "Mail": "comms",
    "Microsoft Outlook": "comms", "WhatsApp": "comms", "‎WhatsApp": "comms",
    "Telegram": "comms", "Discord": "comms",
    "Zoom": "meetings", "Microsoft Teams": "meetings",
    "Finder": "system", "System Preferences": "system",
    "System Settings": "system", "Activity Monitor": "system",
}


def _category(app: str) -> str:
    return _CATEGORIES.get(app, "other")


def _extract_domain(url: str) -> str:
    """Return bare domain from a URL, stripping www. prefix."""
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.") if host else ""
    except Exception:
        return ""


def _timeperiod(d: datetime.date) -> tuple[str, str]:
    """Return (start, end) local-midnight strings for one calendar day."""
    nxt = d + datetime.timedelta(days=1)
    return f"{d.isoformat()}T00:00:00", f"{nxt.isoformat()}T00:00:00"


# ── HTTP client ────────────────────────────────────────────────────────────────

class _AWClient:
    def __init__(self, base: str) -> None:
        self._base = base.rstrip("/")
        self._s = requests.Session()
        self._s.headers["Content-Type"] = "application/json"

    def buckets(self) -> dict:
        r = self._s.get(f"{self._base}/api/0/buckets/", timeout=10)
        r.raise_for_status()
        return r.json()

    def query(self, tp: tuple[str, str], lines: list[str]) -> list:
        body = {
            "timeperiods": [f"{tp[0]}/{tp[1]}"],
            "query": lines,
        }
        r = self._s.post(f"{self._base}/api/0/query/", json=body, timeout=30)
        r.raise_for_status()
        return r.json()


# ── bucket selection ───────────────────────────────────────────────────────────

def _best_bucket(all_buckets: dict, prefix: str) -> str | None:
    """Pick the most-recently-updated bucket whose ID starts with *prefix*."""
    matches = {k: v for k, v in all_buckets.items() if k.startswith(prefix)}
    if not matches:
        return None
    return max(matches, key=lambda k: matches[k].get("last_updated", ""))


def _resolve_buckets(
    all_buckets: dict, configured_host: str
) -> tuple[str | None, str | None, str | None]:
    """
    Return (window_id, afk_id, web_chrome_id).

    If configured_host is set, construct names directly and warn if missing.
    Otherwise pick by most-recently-updated across all hostnames.
    """
    if configured_host:
        win = f"aw-watcher-window_{configured_host}"
        afk = f"aw-watcher-afk_{configured_host}"
        if win not in all_buckets:
            log.warning("Configured bucket %r not found; falling back to auto-detect", win)
            win = _best_bucket(all_buckets, "aw-watcher-window_")
            afk = None
        if afk and afk not in all_buckets:
            log.warning("Configured AFK bucket %r not found; falling back to auto-detect", afk)
            afk = None
    else:
        win = _best_bucket(all_buckets, "aw-watcher-window_")
        afk = None

    # Derive afk from window hostname if still unknown
    if win and afk is None:
        suffix = win[len("aw-watcher-window_"):]
        candidate = f"aw-watcher-afk_{suffix}"
        afk = candidate if candidate in all_buckets else _best_bucket(all_buckets, "aw-watcher-afk_")

    web = _best_bucket(all_buckets, "aw-watcher-web-chrome")

    log.debug("Resolved buckets  window=%s  afk=%s  web=%s", win, afk, web)
    return win, afk, web


# ── raw event helpers ────────────────────────────────────────────────────────

def _fetch_raw_events(
    client: _AWClient,
    win_id: str,
    afk_id: str | None,
    web_id: str | None,
    tp: tuple[str, str],
) -> list[dict]:
    """
    Return raw (non-merged), AFK-filtered window events for *tp*.

    Each event: {app, title, domain, start_iso, end_iso, duration_seconds}
    Domain is populated for browser events by cross-referencing the web-chrome bucket.
    """
    # ── raw window events ──────────────────────────────────────────────────
    lines = [f'window = query_bucket("{win_id}");']
    if afk_id:
        lines += [
            f'afk = query_bucket("{afk_id}");',
            'active = filter_keyvals(afk, "status", ["not-afk"]);',
            'window = filter_period_intersect(window, active);',
        ]
    lines.append('RETURN = window;')

    try:
        result = client.query(tp, lines)
        raw_list = result[0] if result else []
    except Exception as exc:
        log.warning("Raw window events query failed: %s", exc)
        return []

    events: list[dict] = []
    for ev in raw_list:
        ts_str = ev.get("timestamp", "")
        duration = float(ev.get("duration", 0))
        app = ev.get("data", {}).get("app", "").strip()
        title = ev.get("data", {}).get("title", "").strip()

        if not app or not ts_str or duration < 0.5:
            continue
        try:
            start_dt = datetime.datetime.fromisoformat(ts_str)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)
            end_dt = start_dt + datetime.timedelta(seconds=duration)
        except (ValueError, TypeError):
            continue

        events.append({
            "app": app,
            "title": title,
            "domain": None,  # enriched below for browsers
            "start_iso": start_dt.isoformat(),
            "end_iso": end_dt.isoformat(),
            "duration_seconds": duration,
        })

    # ── domain enrichment from web-chrome bucket ──────────────────────────
    _BROWSER_APPS = {
        "Google Chrome", "Chrome", "Chromium",
        "Safari", "Firefox", "Arc", "Brave Browser", "Opera",
    }
    browser_events = [e for e in events if e["app"] in _BROWSER_APPS]

    if web_id and browser_events:
        try:
            web_result = client.query(tp, [
                f'web = query_bucket("{web_id}");',
                'RETURN = web;',
            ])
            web_raw = web_result[0] if web_result else []

            # Build list of (start_epoch, end_epoch, domain)
            web_intervals: list[tuple[float, float, str]] = []
            for wev in web_raw:
                wts = wev.get("timestamp", "")
                wdur = float(wev.get("duration", 0))
                url = wev.get("data", {}).get("url", "")
                dom = _extract_domain(url)
                if not dom or dom.startswith("localhost") or dom.startswith("127."):
                    continue
                try:
                    wstart = datetime.datetime.fromisoformat(wts)
                    if wstart.tzinfo is None:
                        wstart = wstart.replace(tzinfo=datetime.timezone.utc)
                    ws_ep = wstart.timestamp()
                    web_intervals.append((ws_ep, ws_ep + wdur, dom))
                except (ValueError, TypeError):
                    continue

            # For each browser window event, pick the most-overlapping domain
            for ev in browser_events:
                try:
                    ev_s = datetime.datetime.fromisoformat(ev["start_iso"]).timestamp()
                    ev_e = datetime.datetime.fromisoformat(ev["end_iso"]).timestamp()
                except (ValueError, TypeError):
                    continue
                domain_secs: dict[str, float] = {}
                for ws, we, wd in web_intervals:
                    overlap = min(ev_e, we) - max(ev_s, ws)
                    if overlap > 0:
                        domain_secs[wd] = domain_secs.get(wd, 0) + overlap
                if domain_secs:
                    ev["domain"] = max(domain_secs, key=domain_secs.__getitem__)
        except Exception as exc:
            log.debug("Domain enrichment failed: %s", exc)

    events.sort(key=lambda e: e["start_iso"])
    log.debug("_fetch_raw_events: %d events for %s", len(events), tp[0][:10])
    return events


# ── empty / error result ──────────────────────────────────────────────────────

def _empty() -> dict:
    return {
        "by_app": [],
        "by_domain": [],
        "total_active_minutes": 0,
        "total_idle_minutes": 0,
        "raw_events": [],
    }


# ── main collection logic ─────────────────────────────────────────────────────

def get_date(d: datetime.date) -> dict:
    """Return structured AW data for date *d*. Never raises."""
    cfg = get_config()
    base: str = cfg.get("activitywatch_host", "http://localhost:5600")
    tp = _timeperiod(d)

    try:
        client = _AWClient(base)
        all_buckets = client.buckets()
    except requests.exceptions.ConnectionError:
        log.warning("ActivityWatch not running at %s — returning empty data", base)
        return _empty()
    except requests.exceptions.RequestException as exc:
        log.warning("ActivityWatch unreachable (%s) — returning empty data", exc)
        return _empty()

    configured_host: str = cfg.get("activitywatch_hostname", "") or ""
    win_id, afk_id, web_id = _resolve_buckets(all_buckets, configured_host.strip())

    if not win_id:
        log.warning("No aw-watcher-window_ bucket found")
        return _empty()

    # ── 1. app-time breakdown (AFK-filtered) ──────────────────────────────────
    by_app: list[dict] = []
    try:
        result = client.query(tp, [
            f'window = query_bucket("{win_id}");',
            f'afk    = query_bucket("{afk_id}");',
            'active = filter_keyvals(afk, "status", ["not-afk"]);',
            'window = filter_period_intersect(window, active);',
            'window = merge_events_by_keys(window, ["app"]);',
            'RETURN = sort_by_duration(window);',
        ])
        for ev in (result[0] if result else []):
            app = ev.get("data", {}).get("app", "").strip()
            if not app:
                continue
            mins = round(ev["duration"] / 60, 1)
            if mins < 0.1:
                continue
            by_app.append({"app": app, "minutes": mins, "category": _category(app)})
    except Exception as exc:
        log.warning("Window/AFK query failed: %s", exc)

    # ── 2. active / idle totals ───────────────────────────────────────────────
    total_active = 0
    total_idle = 0
    if afk_id:
        try:
            result = client.query(tp, [
                f'afk    = query_bucket("{afk_id}");',
                'active = filter_keyvals(afk, "status", ["not-afk"]);',
                'idle   = filter_keyvals(afk, "status", ["afk"]);',
                'RETURN = {"active_secs": sum_durations(active), "idle_secs": sum_durations(idle)};',
            ])
            totals = result[0] if result else {}
            total_active = math.floor(totals.get("active_secs", 0) / 60)
            total_idle = math.floor(totals.get("idle_secs", 0) / 60)
        except Exception as exc:
            log.warning("AFK totals query failed: %s", exc)

    # ── 3. web domain breakdown (via web-chrome bucket) ───────────────────────
    by_domain: list[dict] = []
    if web_id:
        try:
            result = client.query(tp, [
                f'events = query_bucket("{web_id}");',
                'RETURN = events;',
            ])
            domain_secs: dict[str, float] = {}
            for ev in (result[0] if result else []):
                url = ev.get("data", {}).get("url", "")
                domain = _extract_domain(url)
                if not domain:
                    continue
                # Skip localhost / loopback (AW dashboard etc.)
                if domain.startswith("localhost") or domain.startswith("127."):
                    continue
                domain_secs[domain] = domain_secs.get(domain, 0) + ev.get("duration", 0)

            by_domain = sorted(
                [
                    {"domain": dom, "minutes": round(secs / 60, 1)}
                    for dom, secs in domain_secs.items()
                    if round(secs / 60, 1) >= 0.1
                ],
                key=lambda x: x["minutes"],
                reverse=True,
            )
        except Exception as exc:
            log.warning("Web-chrome query failed: %s", exc)

    # ── 4. raw events (for timeline builder) ─────────────────────────────────
    raw_events: list[dict] = []
    try:
        raw_events = _fetch_raw_events(client, win_id, afk_id, web_id, tp)
    except Exception as exc:
        log.warning("Raw events fetch failed: %s", exc)

    return {
        "by_app": by_app,
        "by_domain": by_domain,
        "total_active_minutes": total_active,
        "total_idle_minutes": total_idle,
        "raw_events": raw_events,
    }


def get_today() -> dict:
    return get_date(datetime.date.today())


def get_events_date(date: datetime.date) -> list[dict]:
    """
    Return raw AFK-filtered window events for *date* (standalone, no summary data).

    Each event: {app, title, domain, start_iso, end_iso, duration_seconds}
    Never raises — returns [] on connection failure.
    """
    cfg = get_config()
    base: str = cfg.get("activitywatch_host", "http://localhost:5600")
    tp = _timeperiod(date)

    try:
        client = _AWClient(base)
        all_buckets = client.buckets()
    except requests.exceptions.ConnectionError:
        log.warning("ActivityWatch not running at %s — returning empty events", base)
        return []
    except requests.exceptions.RequestException as exc:
        log.warning("ActivityWatch unreachable (%s) — returning empty events", exc)
        return []

    configured_host: str = cfg.get("activitywatch_hostname", "") or ""
    win_id, afk_id, web_id = _resolve_buckets(all_buckets, configured_host.strip())

    if not win_id:
        log.warning("No aw-watcher-window_ bucket found")
        return []

    return _fetch_raw_events(client, win_id, afk_id, web_id, tp)


def get_events_today() -> list[dict]:
    """Return raw events for today. See get_events_date()."""
    return get_events_date(datetime.date.today())


# ── CLI ────────────────────────────────────────────────────────────────────────

def _bar(value: float, max_val: float, width: int = 18) -> str:
    if max_val <= 0:
        return ""
    filled = round((value / max_val) * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_mins(mins: float) -> str:
    h, m = divmod(int(mins), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _cli() -> None:
    parser = argparse.ArgumentParser(description="ActivityWatch collector dry-run")
    parser.add_argument("--date", default=str(datetime.date.today()),
                        help="YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary tables")
    args = parser.parse_args()

    try:
        d = datetime.date.fromisoformat(args.date)
    except ValueError:
        print(f"Invalid date: {args.date!r}", file=sys.stderr)
        sys.exit(1)

    data = get_date(d)

    if not args.dry_run:
        import json
        print(json.dumps(data, indent=2))
        return

    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console()

    # ── summary header ────────────────────────────────────────────────────────
    active = data["total_active_minutes"]
    idle = data["total_idle_minutes"]
    console.print(
        f"\n[bold]ActivityWatch — {d}[/]  |  "
        f"Active: [green]{_fmt_mins(active)}[/]  "
        f"Idle: [dim]{_fmt_mins(idle)}[/]\n"
    )

    # ── by_app table ──────────────────────────────────────────────────────────
    by_app = data["by_app"]
    if by_app:
        max_mins = by_app[0]["minutes"]  # already sorted desc
        t = Table(title="Time by App", show_lines=False)
        t.add_column("App", style="cyan", no_wrap=True)
        t.add_column("Time", justify="right", style="bright_green")
        t.add_column("", no_wrap=True)          # bar
        t.add_column("%", justify="right")
        t.add_column("Category", style="magenta")

        for row in by_app:
            pct = (row["minutes"] / active * 100) if active else 0
            t.add_row(
                row["app"],
                _fmt_mins(row["minutes"]),
                _bar(row["minutes"], max_mins),
                f"{pct:.0f}%",
                row["category"],
            )
        console.print(t)

    # ── by_domain table ───────────────────────────────────────────────────────
    by_domain = data["by_domain"]
    if by_domain:
        max_dom = by_domain[0]["minutes"]
        t2 = Table(title="Time by Domain (browser)", show_lines=False)
        t2.add_column("Domain", style="cyan", no_wrap=True)
        t2.add_column("Time", justify="right", style="bright_green")
        t2.add_column("", no_wrap=True)

        for row in by_domain[:20]:  # cap at 20 rows
            t2.add_row(
                row["domain"],
                _fmt_mins(row["minutes"]),
                _bar(row["minutes"], max_dom),
            )
        console.print(t2)

    if not by_app and not by_domain:
        console.print("[yellow]No data found for this date.[/]")


if __name__ == "__main__":
    _cli()
