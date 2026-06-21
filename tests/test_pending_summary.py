import datetime
import json
from unittest.mock import patch

import pytest


def test_save_pending_uses_active_status(tmp_path):
    from pipeline.pending_summary import save_pending, load_pending
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending({"done": [], "unfinished": [], "unclassified": []}, datetime.date(2026, 6, 20))
        record = load_pending(datetime.date(2026, 6, 20))
    assert record["status"] == "active"


def test_update_current_replaces_classification(tmp_path):
    from pipeline.pending_summary import save_pending, update_current, get_current
    date = datetime.date(2026, 6, 20)
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending({"done": [], "unfinished": [], "unclassified": []}, date)
        new_classification = {"done": [{"task_name": "Done A"}], "unfinished": [], "unclassified": []}
        result = update_current(new_classification, date)
    assert result["done"][0]["task_name"] == "Done A"


def test_save_reviewed_sets_status(tmp_path):
    from pipeline.pending_summary import save_pending, save_reviewed, load_pending
    date = datetime.date(2026, 6, 20)
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending({"done": [], "unfinished": [], "unclassified": []}, date)
        save_reviewed(date)
        record = load_pending(date)
    assert record["status"] == "reviewed"
