"""
Google Calendar collector — stub.

Returns [] until Google Calendar API credentials are configured.

Future schema when implemented:
    [{"title": str, "start_time": str, "end_time": str, "duration_minutes": int}]
"""
from __future__ import annotations


def get_tomorrow_events() -> list[dict]:
    """Return tomorrow's calendar events. Stub: always returns []."""
    return []
