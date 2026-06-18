#!/usr/bin/env python3
"""
One-time script: add Target Count + Recurrence properties to the Tasks Notion DB.

Idempotent — skips properties that already exist.

Usage:
    python scripts/add_task_fields.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_loader import get_config
from utils.notion_client import get_notion

FIELDS_TO_ADD: dict[str, dict] = {
    "Target Count": {"number": {"format": "number"}},
    "Recurrence": {
        "select": {
            "options": [
                {"name": "None", "color": "default"},
                {"name": "Daily", "color": "blue"},
                {"name": "Weekly", "color": "purple"},
            ]
        }
    },
}


def main() -> None:
    cfg = get_config()
    db_id: str = cfg["notion_tasks_db_id"]
    notion = get_notion()

    db = notion.databases.retrieve(database_id=db_id)
    existing = set(db.get("properties", {}).keys())

    to_add: dict[str, dict] = {}
    for name, schema in FIELDS_TO_ADD.items():
        if name in existing:
            print(f"SKIP  {name!r} — already exists")
        else:
            to_add[name] = schema
            print(f"ADD   {name!r}")

    if not to_add:
        print("\nAll properties present — nothing to do.")
        return

    notion.databases.update(database_id=db_id, properties=to_add)
    print(f"\nUpdated Tasks DB ({db_id}): added {len(to_add)} property/properties.")


if __name__ == "__main__":
    main()
