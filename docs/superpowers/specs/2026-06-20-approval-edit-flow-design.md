# Approval & Edit Flow — Design Spec

**Date:** 2026-06-20
**Status:** Approved

---

## Problem

The current approval flow requires the user to reply to a Telegram message to approve or edit the daily summary. This is inconvenient on mobile — no drag-and-drop, no inline text editing. Additionally, Notion is only written after explicit approval, meaning a missed review results in no data in Notion at all.

---

## Core Philosophy

**Write-immediately, edit-anytime.** The pipeline writes to Notion the moment it runs. Both Telegram chat and the web UI are edit surfaces that update Notion when the user approves in the UI or sends a chat command. There is no blocking approval gate — Notion always has the latest state.

---

## User Workflow

1. Evening: pipeline runs, writes to Notion, saves `pending-YYYY-MM-DD.json`, sends Telegram message
2. User sees Telegram message with full summary + plan + a link to the web UI
3. User can:
   - **Path A (Telegram chat):** Reply with natural language ("move task X to unfinished") → agent edits pending JSON + updates Notion + replies with confirmation
   - **Path B (Web UI):** Tap the link → edit tasks via drag/drop and inline text editing → tap "Approve" → full state POSTed → Notion updated + Telegram confirmation sent
4. If the user never reviews: Notion already has the pipeline's output, nothing is lost

---

## Data Flow

```
Pipeline runs
  ├─ Write daily summary + tomorrow's plan to Notion
  ├─ Save pending-YYYY-MM-DD.json (status: "active")
  └─ Send Telegram:
       - Full summary text (existing format)
       - Tomorrow's plan text (existing format)
       - Footer: "Edit in UI → http://<proxmox-ip>:<port>/review/YYYY-MM-DD"

Path A — Telegram chat
  → telegram_webhook.py detects edit intent
  → apply_edit() updates pending JSON
  → updates Notion in-place
  → sends Telegram reply confirming change

Path B — Web UI
  → User edits locally in browser (no network calls during editing)
  → Taps "Approve"
  → POST /api/review/{date}/approve with full updated state
  → Updates pending JSON (status: "reviewed")
  → Updates Notion
  → Sends Telegram: "✓ Reviewed for YYYY-MM-DD"
```

---

## Web UI

**Served at:** `GET /review/{date}` — single mobile-optimized HTML page

**Daily Review section:**
- Three buckets: Done ✅ / Unfinished 🔄 / Unclassified ❓
- Draggable task cards — drag between buckets to reclassify
- Tap card to edit name inline
- Tap card to expand/collapse evidence/reason

**Tomorrow's Plan section:**
- Grouped by project
- Each item is an editable text line
- Tap to edit, delete button per item, "+" to add task under a project

**Bottom bar:**
- "Approve" button — submits full state, updates Notion, sends Telegram confirmation

**Auth:** None. URL contains the date. Proxmox home network access is sufficient.

---

## Backend Structure

```
web/
  server.py          # FastAPI app
  static/
    index.html       # SPA — HTML + vanilla JS + CSS
```

**Routes:**
- `GET /review/{date}` — serves index.html
- `GET /api/review/{date}` — returns pending JSON for that date
- `POST /api/review/{date}/approve` — receives full edited state, writes pending JSON, updates Notion, sends Telegram confirmation

**Existing code reused:**
- `pipeline/pending_summary.py` — load/save pending JSON
- `collectors/collect_tasks.py` — `mark_task_done()` for Notion writes
- `delivery/telegram_send.py` — send Telegram confirmation

---

## Changes to Existing Code

### `pipeline/run_daily.py`
- Write to Notion immediately after classification (remove approval gate)
- Save pending JSON with `status: "active"` instead of `"awaiting_approval"`

### `delivery/telegram_webhook.py`
- Remove `awaiting_approval` status check before applying edits
- After `apply_edit()`, update Notion in-place
- Telegram message footer: append UI link

### `pipeline/pending_summary.py`
- Replace `status: "awaiting_approval"` with `status: "active"`
- `save_approved()` → rename to `save_reviewed()`, sets `status: "reviewed"`
- Remove expiry-gated Notion write logic (Notion is written by pipeline directly)

---

## Deployment

The web server runs as a separate process on the user's Proxmox container alongside the existing daily agent code. Deployment instructions will be provided as part of the implementation plan (clone repo, install deps, run `uvicorn web.server:app`).

**Port:** configurable in `config.yaml` (e.g., `web_port: 8080`)
**UI URL pattern:** `http://<proxmox-ip>:<web_port>/review/YYYY-MM-DD`

---

## Out of Scope

- Authentication / access control
- Real-time sync between multiple devices viewing the UI simultaneously
- Chat interface within the web UI (chat remains Telegram-only)
