"""FastAPI web server for daily review UI."""
from __future__ import annotations
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from utils.logger import get_logger

log = get_logger("web.server")

_STATIC_DIR = pathlib.Path(__file__).parent / "static"

app = FastAPI(title="Daily Agent Review")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


class ApproveRequest(BaseModel):
    classification: dict
    plan: list[dict]


@app.get("/review/{date}")
def review_page(date: str):
    """Serve the SPA."""
    index = _STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=500, detail="index.html not found")
    return FileResponse(str(index), media_type="text/html")


@app.get("/api/review/{date}")
def get_review(date: str):
    """Return classification + plan for the given date."""
    try:
        parsed_date = datetime.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    from pipeline.pending_summary import get_current
    from pipeline.plan_store import load_plan

    classification = get_current(parsed_date) or {}
    tomorrow = parsed_date + datetime.timedelta(days=1)
    plan = load_plan(tomorrow) or []

    return JSONResponse({"classification": classification, "plan": plan, "date": date})


@app.post("/api/review/{date}/approve")
def approve_review(date: str, body: ApproveRequest):
    """Save edited state, sync to Notion, send Telegram confirmation."""
    try:
        parsed_date = datetime.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    from pipeline.pending_summary import update_current, save_reviewed
    from pipeline.plan_store import save_plan
    from pipeline.notion_sync import write_classification_to_notion
    from delivery.telegram_send import send_text

    # 1. Update pending JSON with edited classification
    try:
        update_current(body.classification, parsed_date)
    except Exception as exc:
        log.warning("update_current failed: %s", exc)

    # 2. Mark as reviewed
    try:
        save_reviewed(parsed_date)
    except Exception as exc:
        log.warning("save_reviewed failed: %s", exc)

    # 3. Update plan_store with edited plan
    tomorrow = parsed_date + datetime.timedelta(days=1)
    try:
        save_plan(body.plan, tomorrow)
    except Exception as exc:
        log.warning("save_plan failed: %s", exc)

    # 4. Sync classification to Notion
    try:
        write_classification_to_notion(body.classification, parsed_date)
    except Exception as exc:
        log.warning("Notion sync failed: %s", exc)

    # 5. Send Telegram confirmation
    try:
        send_text(f"✓ Reviewed for {parsed_date} (via UI).")
    except Exception as exc:
        log.warning("Telegram confirmation failed: %s", exc)

    return {"status": "ok", "date": date}


def main():
    import uvicorn
    from config_loader import get_config
    cfg = get_config()
    port = int(cfg.get("web_port", 8080))
    uvicorn.run("web.server:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
