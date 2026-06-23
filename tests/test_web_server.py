import datetime, json
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

DATE = "2026-06-20"
PENDING = {
    "done": [{"task_name": "Task A", "page_id": "abc"}],
    "unfinished": [],
    "unclassified": [],
    "day_theme": "Productive day",
}
PLAN = [{"id": 1, "task": "Build UI", "priority": "high", "context": "needed", "project": "Daily Agent"}]


@pytest.fixture
def client():
    from web.server import app
    return TestClient(app)


def test_get_review_api(client):
    with patch("pipeline.pending_summary.get_current", return_value=PENDING), \
         patch("pipeline.plan_store.load_plan", return_value=PLAN):
        resp = client.get(f"/api/review/{DATE}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["classification"]["day_theme"] == "Productive day"
    assert data["plan"][0]["task"] == "Build UI"


def test_approve_endpoint(client):
    payload = {"classification": PENDING, "plan": PLAN}
    with patch("pipeline.pending_summary.update_current", return_value=PENDING) as mock_uc, \
         patch("pipeline.pending_summary.save_reviewed") as mock_sr, \
         patch("pipeline.plan_store.save_plan") as mock_sp, \
         patch("pipeline.notion_sync.write_classification_to_notion") as mock_wn, \
         patch("delivery.telegram_send.send_text") as mock_st:
        resp = client.post(f"/api/review/{DATE}/approve", json=payload)
    assert resp.status_code == 200
    mock_uc.assert_called_once()
    mock_sr.assert_called_once()
    mock_wn.assert_called_once()
    mock_st.assert_called_once()


def test_get_review_page_serves_html(client):
    resp = client.get(f"/review/{DATE}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
