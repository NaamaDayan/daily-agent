import datetime
from unittest.mock import patch, call
from pipeline.notion_sync import write_classification_to_notion

CLASSIFICATION = {
    "done": [{"task_name": "Task A", "page_id": "abc123"}],
    "unfinished": [{"task_name": "Task B"}],
    "unclassified": [],
}

def test_marks_done_tasks():
    with patch("collectors.collect_tasks.mark_task_done") as mock_mark, \
         patch("context.update_context.upsert_daily_entry"):
        write_classification_to_notion(CLASSIFICATION, datetime.date(2026, 6, 20))
    mock_mark.assert_called_once_with("abc123")

def test_upserts_daily_entry():
    with patch("collectors.collect_tasks.mark_task_done"), \
         patch("context.update_context.upsert_daily_entry") as mock_upsert:
        write_classification_to_notion(CLASSIFICATION, datetime.date(2026, 6, 20))
    mock_upsert.assert_called_once()
    args, kwargs = mock_upsert.call_args
    assert args[0] == datetime.date(2026, 6, 20)
    assert "actual" in kwargs

def test_skips_task_without_page_id():
    no_page_id = {"done": [{"task_name": "Task A"}], "unfinished": [], "unclassified": []}
    with patch("collectors.collect_tasks.mark_task_done") as mock_mark, \
         patch("context.update_context.upsert_daily_entry"):
        write_classification_to_notion(no_page_id, datetime.date(2026, 6, 20))
    mock_mark.assert_not_called()

def test_notion_failure_is_nonfatal():
    with patch("collectors.collect_tasks.mark_task_done", side_effect=Exception("boom")), \
         patch("context.update_context.upsert_daily_entry"):
        # should not raise
        write_classification_to_notion(CLASSIFICATION, datetime.date(2026, 6, 20))
