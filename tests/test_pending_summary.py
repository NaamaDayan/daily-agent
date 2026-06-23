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


def test_apply_edit_accepts_active_status(tmp_path):
    """apply_edit() should accept both 'active' and 'awaiting_approval' status."""
    from pipeline.pending_summary import save_pending, apply_edit, load_pending
    date = datetime.date(2026, 6, 20)
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending(
            {
                "done": [],
                "unfinished": [{"task_name": "Task A"}],
                "unclassified": []
            },
            date
        )
        record = load_pending(date)
        assert record["status"] == "active"

        # Mock the LLM call to avoid actual API calls
        with patch("pipeline.pending_summary._apply_edit_with_llm") as mock_llm:
            mock_llm.return_value = {
                "done": [{"task_name": "Task A"}],
                "unfinished": [],
                "unclassified": []
            }
            # Should not raise error despite status being "active"
            result = apply_edit("Task A is done", date)
            assert result is not None
            assert len(result["done"]) == 1


def test_apply_edit_accepts_awaiting_approval_status(tmp_path):
    """apply_edit() should also accept 'awaiting_approval' status."""
    from pipeline.pending_summary import save_pending, load_pending, apply_edit
    date = datetime.date(2026, 6, 20)
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending(
            {
                "done": [],
                "unfinished": [{"task_name": "Task B"}],
                "unclassified": []
            },
            date
        )
        # Manually set status to awaiting_approval for this test
        record = load_pending(date)
        record["status"] = "awaiting_approval"
        import pathlib
        path = pathlib.Path(tmp_path) / f"pending-{date.isoformat()}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        with patch("pipeline.pending_summary._apply_edit_with_llm") as mock_llm:
            mock_llm.return_value = {
                "done": [{"task_name": "Task B"}],
                "unfinished": [],
                "unclassified": []
            }
            result = apply_edit("Task B is done", date)
            assert result is not None
            assert len(result["done"]) == 1


def test_apply_edit_rejects_other_statuses(tmp_path):
    """apply_edit() should reject pending records with other statuses."""
    from pipeline.pending_summary import save_pending, load_pending, apply_edit
    date = datetime.date(2026, 6, 20)
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending({"done": [], "unfinished": [], "unclassified": []}, date)
        record = load_pending(date)
        record["status"] = "reviewed"
        import pathlib
        path = pathlib.Path(tmp_path) / f"pending-{date.isoformat()}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(ValueError, match="not active or awaiting approval"):
            apply_edit("some edit", date)


def test_is_expired_skips_reviewed_status(tmp_path):
    """is_expired() should skip expiry check for 'reviewed' status."""
    from pipeline.pending_summary import save_pending, load_pending, is_expired
    import datetime as dt

    date = datetime.date(2026, 6, 20)
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending({"done": [], "unfinished": [], "unclassified": []}, date)
        record = load_pending(date)
        # Set created_at to 40 hours ago (should be expired, but we skip it for reviewed status)
        now = dt.datetime.now(tz=dt.timezone.utc)
        old_time = now - dt.timedelta(hours=40)
        record["created_at"] = old_time.isoformat(timespec="seconds")
        record["status"] = "reviewed"
        import pathlib
        path = pathlib.Path(tmp_path) / f"pending-{date.isoformat()}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        # Should return False (not expired) because status is reviewed
        assert is_expired(date) is False


def test_is_expired_skips_approved_status(tmp_path):
    """is_expired() should skip expiry check for 'approved' status."""
    from pipeline.pending_summary import save_pending, load_pending, is_expired
    import datetime as dt

    date = datetime.date(2026, 6, 20)
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending({"done": [], "unfinished": [], "unclassified": []}, date)
        record = load_pending(date)
        # Set created_at to 40 hours ago
        now = dt.datetime.now(tz=dt.timezone.utc)
        old_time = now - dt.timedelta(hours=40)
        record["created_at"] = old_time.isoformat(timespec="seconds")
        record["status"] = "approved"
        import pathlib
        path = pathlib.Path(tmp_path) / f"pending-{date.isoformat()}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        # Should return False (not expired) because status is approved
        assert is_expired(date) is False


def test_is_expired_returns_true_for_old_active_records(tmp_path):
    """is_expired() should return True for old 'active' status records."""
    from pipeline.pending_summary import save_pending, load_pending, is_expired
    import datetime as dt

    date = datetime.date(2026, 6, 20)
    with patch("pipeline.pending_summary._plans_dir", return_value=tmp_path):
        save_pending({"done": [], "unfinished": [], "unclassified": []}, date)
        record = load_pending(date)
        # Set created_at to 40 hours ago
        now = dt.datetime.now(tz=dt.timezone.utc)
        old_time = now - dt.timedelta(hours=40)
        record["created_at"] = old_time.isoformat(timespec="seconds")
        # status remains "active" (default from save_pending)
        import pathlib
        path = pathlib.Path(tmp_path) / f"pending-{date.isoformat()}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        # Should return True because status is active and age > 36 hours
        assert is_expired(date) is True
