"""Shared, lazily-initialised Notion SDK client."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from functools import lru_cache

from notion_client import Client

from config_loader import get_config


@lru_cache(maxsize=1)
def get_notion() -> Client:
    cfg = get_config()
    return Client(auth=cfg["notion_api_key"])
