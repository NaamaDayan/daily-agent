# Daily Agent — Architecture & Build Guide

## What This System Does

A personal productivity agent that runs on your Mac, silently collects what you
actually did today (typed text, active apps, meeting transcripts, Cursor sessions),
classifies activity against your Notion Tasks DB, names unclassified time, and
delivers a slim summary to Telegram at end of day. You reply to edit and approve;
only approved summaries are written to Notion. A self-learning loop injects past
approved classifications as few-shot examples into future runs. Everything runs
locally except Claude API calls (~4–6 per day at ~$0.02–0.05 total).

---

## Project Structure

```
daily-agent/
├── CLAUDE.md                        ← you are here
├── README.md
│
├── daemon/                          ← ALREADY BUILT (do not modify)
│   └── typing-daemon                ← macOS Swift/Python daemon
│       Writes: ~/.typing-log/YYYY-MM-DD.json
│       Schema: [{timestamp, app, bundle_id, window_title, text}]
│
├── collectors/
│   ├── collect_typing.py            ← reads typing logs, filters + dedupes
│   ├── collect_activitywatch.py     ← queries ActivityWatch REST API on localhost:5600
│   ├── collect_cursor.py            ← reads Cursor SQLite sessions
│   ├── collect_notion_meetings.py   ← queries Notion API for today's meeting summaries
│   └── collect_tasks.py             ← reads Notion Tasks + Projects DBs
│
├── pipeline/
│   ├── run_daily.py                 ← main entry point, called by cron/OpenClaw at 20:00
│   ├── summarizer.py                ← timeline builder (Stage 1A/1B) + narrate (Stage 2)
│   ├── classifier.py                ← matches timeline segments to Notion tasks (Stage 1C)
│   ├── activity_namer.py            ← clusters + names unclassified segments
│   ├── pending_summary.py           ← iterative approval storage + edit application
│   ├── learning_store.py            ← records approved days; few-shot for classifier
│   ├── micro_summarizer.py          ← optional: runs every 30min during the day
│   └── plan_store.py                ← persists + updates tomorrow's plan (JSON file)
│
├── scripts/
│   └── add_task_fields.py           ← one-time: add Target Count + Recurrence to Tasks DB
│
├── utils/
│   ├── cost_logger.py               ← JSONL API cost log
│   └── token_logger.py              ← CLI: --today --type stage1_classify
│
├── delivery/                        ← Telegram integration
│   ├── telegram_send.py             ← sends formatted message via Bot API
│   └── telegram_webhook.py          ← receives replies, parses plan edits, confirms
│
├── context/                         ← user-controlled context (pulled from Notion)
│   └── fetch_context.py             ← reads the "Agent Context" Notion page
│
├── claw.md                          ← OpenClaw skill definition (cron + Telegram wiring)
│
├── config.yaml                      ← all configuration (API keys, paths, Notion IDs)
├── requirements.txt
└── logs/
    └── agent-YYYY-MM-DD.log
```

---

## Data Sources & Schemas

### 1. Typing Daemon Output (ALREADY EXISTS — READ ONLY)
**Location:** `~/.typing-log/YYYY-MM-DD.json`

```json
[
  {
    "timestamp": "2026-05-29T14:32:11Z",
    "app": "Google Chrome",
    "bundle_id": "com.google.Chrome",
    "window_title": "Claude - New conversation",
    "text": "what is the best architecture for a daily summarization agent"
  }
]
```

**Key bundle IDs to watch:**
- `com.google.Chrome` / `com.apple.Safari` — browser (check window_title for domain)
- `com.todesktop.230313mzl4w4u92` — Cursor IDE
- `io.claude.app` — Claude desktop (if used)
- `com.apple.Notes` — Notes.app
- `com.microsoft.Word` / `com.microsoft.Powerpoint`
- `com.notion.mac` — Notion desktop

### 2. ActivityWatch (time-tracking)
**Source:** `http://localhost:5600/api/0/buckets/`

Relevant buckets:
- `aw-watcher-window_<hostname>` — active app + window title, 1-second resolution
- `aw-watcher-web-chrome` — active Chrome tab URL + title
- `aw-watcher-afk_<hostname>` — idle/active detection

Query endpoint: `GET /api/0/query/`

```python
# Example: get today's app time
{
  "timeperiods": ["2026-05-29T00:00:00/2026-05-30T00:00:00"],
  "query": [
    "events = query_bucket(find_bucket('aw-watcher-window_'));",
    "events = filter_keyvals(events, 'status', ['not-afk']);",
    "events = merge_events_by_keys(events, ['app', 'title']);",
    "RETURN = sort_by_duration(events);"
  ]
}
```

Returns array of `{app, title, duration_seconds}`.

### 3. Cursor IDE Sessions
**Location:** `~/.cursor/User/workspaceStorage/*/`

Two SQLite DBs to check per workspace:
- `backup.db` → table `composer.composerData` (JSON blob) → current sessions
- `state.vscdb` → table `ItemTable` where key = `'workbench.panel.aichat.view.aichat.chatdata'`

Use `cursor-history` CLI if installed (`npm install -g cursor-history`):
```bash
cursor-history export --since today --format json -o /tmp/cursor-today.json
```

Or query directly:
```python
import sqlite3, json, glob, pathlib
from datetime import date

workspaces = pathlib.Path.home() / ".cursor/User/workspaceStorage"
for db_path in workspaces.glob("*/backup.db"):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT value FROM ItemTable WHERE key = 'composer.composerData'").fetchall()
    # parse JSON, filter by today's date
```

Schema after parsing:
```json
{
  "sessions": [{
    "sessionId": "...",
    "createdAt": 1748476800000,
    "conversation": [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."}
    ]
  }]
}
```

### 4. Notion — Meeting Transcripts
**Source:** Notion API via MCP or direct REST

Meetings DB page properties expected:
- `Date` (date property) — filter by today
- `Summary` (rich text) — the transcript summary to inject
- `Title` (title property) — meeting name

```python
# Filter today's meetings
notion.databases.query(
    database_id=config["notion_meetings_db_id"],
    filter={"property": "Date", "date": {"equals": str(date.today())}}
)
```

### 5. Notion — Agent Context Page
**Source:** Notion page (single page, not a DB)

Expected structure (rich text blocks):
```
# Current Focus
[What I'm working on right now — updated manually]

# Active Projects  
[List of projects with brief descriptions]

# This Week's Goals
[Bullet list]

# Background
[Longer context about role, startup stage, etc.]
```

Read via: `notion.pages.retrieve(page_id=config["notion_context_page_id"])`
Then extract all block children as plain text.

---

## Daily Pipeline Logic (`run_daily.py`)

```
run_daily.py is called at 20:00 daily (by cron or OpenClaw)

Step 1 — Collect (parallel)
  typing_entries  = collect_typing.load_date(date)
  activitywatch   = collect_activitywatch.get_date(date)
  cursor_sessions = collect_cursor.get_date(date)
  meetings        = collect_notion_meetings.get_date(date)
  context         = fetch_context.load()                 # general + today_tasks + projects

Step 2 — Summarize (summarizer.summarize)
  Stage 1A  presummary_all_cursor()       # haiku per Cursor session
  Stage 1B  build_timeline()            # AW events + typing + cursor summaries
  Stage 1C  classifier.classify()       # match segments to Notion tasks (Sonnet)
            activity_namer.name_unclassified()  # cluster + name leftovers (Haiku)
  Stage 2   narrate()                   # day_theme headline from classification (Sonnet)

Step 3 — Store pending summary
  pending_summary.save_pending(classification)   # plans_dir/pending-YYYY-MM-DD.json
  # Notion writes DEFERRED until user approves via Telegram

Step 4 — Deliver
  telegram_send.send_classification(result, date)
  # Footer: "Reply to edit items, or say approve to save to Notion."

Step 5 — Log
  write agent log with token usage, timing, any errors
```

### Approval loop (after delivery)

While a pending summary exists (`status: awaiting_approval`), **every Telegram reply**
is treated as either an edit or final approval (see `claw.md`). No special prefix needed.

```
User reply
  → telegram_webhook.handle_approval_reply()
      if exact match on "approve", "looks good", "👍", etc.
          pending_summary.save_approved()
          learning_store.record_approved_day()   # self-learning MVP
          mark_task_done() for each done task
          upsert_daily_entry(actual=formatted classification)
      else
          pending_summary.apply_edit(instruction)   # Haiku, call_type: approval_edit
          send updated summary + "v{N} — reply to keep editing or approve"
```

Pending file schema: `plans_dir/pending-YYYY-MM-DD.json`
- `current`: working classification `{done, unfinished, unclassified, day_theme, ...}`
- `history`: audit trail of each edit snapshot
- `version`: increments on each edit; `version_count` stored in learning record

---

## Task Classification (`pipeline/classifier.py`)

Matches timeline segments (≥3 min) and meetings to today's Notion tasks using
observable evidence only. Confidence thresholds applied **deterministically in Python**
(`post_process()`), not by the LLM.

### Config thresholds
```yaml
classification_done_threshold: 0.80    # confidence >= this → done
classification_min_threshold: 0.40    # confidence < this → no footprint
```

### Tasks DB fields (via `collect_tasks.get_classifiable_tasks()`)
- `Target Count` (number, default 1) — recurring tasks need N instances
- `Recurrence` (select: None / Daily / Weekly)

### Classifier output schema
```json
{
  "done": [{"task_name", "page_id", "project", "confidence_score",
            "instances_found", "instances_required", "evidence", "time_ranges"}],
  "unfinished": [{"task_name", "page_id", "project", "confidence_score",
                  "reason", "instances_found", "instances_required"}],
  "unmatched_segments": [{"start", "end", "duration_minutes", "app", "domain",
                          "typing", "cursor_summary"}]
}
```

### post_process() rules
- Task not in LLM output → unfinished, reason="No digital footprint detected"
- score ≥ DONE_THRESHOLD → done
- score ≥ MIN_THRESHOLD → unfinished with auto reason
- score < MIN_THRESHOLD → unfinished + claimed segments returned to unmatched_segments

### Few-shot injection (self-learning)
`build_classification_prompt()` prepends past approved examples from `learning_store`
before `=== TASKS ===` when `learning_enabled: true` and records exist.
Token log: `stage1_classify` (Sonnet, max_tokens=2000).

---

## Unclassified Activity Namer (`pipeline/activity_namer.py`)

Receives `unmatched_segments` from the classifier. Clusters deterministically, then
names all clusters in **one Haiku call**.

### Clustering (two-pass, no LLM)
```yaml
activity_cluster_gap_minutes: 5       # merge same-app segments within this gap
activity_cluster_keyword_overlap: 2   # min shared keywords to merge across apps
```

Pass 1: merge consecutive same-app/same-domain segments if gap < 5 min.
Pass 2: merge clusters sharing ≥2 keywords if within 60 min (conservative).

Clusters with total duration < 3 min are dropped. Token log: `activity_naming`.

Output feeds `unclassified_activities` in the summary result:
```json
{"suggested_name", "time_range", "duration_minutes", "description", "category", "app"}
```

---

## Self-Learning MVP (`pipeline/learning_store.py`)

On approval, writes `learning_dir/YYYY-MM-DD.json` with structured signal:
- Per-task: outcome (done/unfinished), confidence, evidence_patterns, had_digital_footprint
- Day metadata: version_count, unclassified_count, total_active_minutes
- Future slots (null for now): work_start_time, focus_blocks, task_durations, edit_corrections

```yaml
learning_dir: "~/.daily-agent/learning"
learning_few_shot_count: 5
learning_enabled: true
```

`get_few_shot_examples()` → N newest records.
`format_few_shot_for_prompt()` → injected into classifier prompt, capped at 800 tokens.

---

## Narration (`summarizer.narrate()`)

Second Sonnet call after classification. Input: done/unfinished/unclassified lists + context.
Output: `{"day_theme": "1-2 sentence headline"}`. Token log: `narrate`.

---

## Legacy synthesis path

`summarizer.build_prompt()` + full narrative synthesis still exists for `--test` CLI
and plan-edit flows, but the daily pipeline uses classify → narrate instead.

---

## Prompt Engineering (legacy synthesis — `summarizer.build_prompt()`)

The daily pipeline no longer uses this path. Kept for `--test` CLI and reference.
The active prompts live in `classifier.py`, `activity_namer.py`, `summarizer.narrate()`,
and `pending_summary._apply_edit_with_llm()`.

### API call types (cost log)
| call_type | Module | Model |
|---|---|---|
| `cursor_presummary` | summarizer Stage 1A | Haiku |
| `stage1_classify` | classifier | Sonnet |
| `activity_naming` | activity_namer | Haiku |
| `narrate` | summarizer Stage 2 | Sonnet |
| `approval_edit` | pending_summary | Haiku |
| `synthesis` | legacy summarizer | Sonnet |

### Legacy system prompt
```
You are a personal productivity assistant with access to everything the user
actually did today on their computer. Your job is to:
1. Write a concise, semantic daily summary — what was actually accomplished,
   not just what apps were used.
2. Generate a prioritized plan for tomorrow based on today's output,
   unfinished work, and the user's stated goals.

Rules:
- Be direct and specific. Name actual topics, projects, decisions.
- Infer intent from context: "spent 45 min in claude.ai asking about
  architecture" → "Designed system architecture for daily agent project"
- For the plan, output 5-7 concrete tasks, ordered by priority.
- Output strictly valid JSON matching the schema below.
```

### User Prompt Structure (built by `build_prompt()`)
```
TODAY'S DATE: {date}

=== YOUR CONTEXT ===
{context_page_text}

=== TIME BREAKDOWN ===
{pie_data_as_text}
e.g. "Chrome: 3h 20min | Cursor: 2h 10min | Notion: 45min | ..."

=== WHAT YOU TYPED (by app) ===

[claude.ai / ChatGPT / Gemini]
{typing entries for AI tools — your prompts only}

[Cursor IDE]
{cursor user turns from today's sessions}

[Notes / Notion / Word]
{typing entries for writing apps}

[Other apps]
{brief: app name + total typed chars as proxy}

=== MEETINGS TODAY ===
{notion meeting summaries, or "No meetings logged today"}

=== OUTPUT SCHEMA ===
Return ONLY valid JSON:
{
  "summary": "3-5 paragraph narrative of what was accomplished today",
  "highlights": ["key accomplishment 1", "key accomplishment 2", ...],
  "tomorrow_plan": [
    {"id": 1, "task": "...", "priority": "high|medium|low", "context": "why"},
    ...
  ],
  "time_breakdown": [
    {"app": "Chrome", "minutes": 200, "category": "research"},
    ...
  ],
  "blockers": ["anything that seems stuck or unresolved"]
}
```

### Token Budget
- Context page: ~800 tokens
- Time breakdown: ~200 tokens
- Typing entries (filtered): ~1500-2500 tokens
- Cursor sessions (user turns only): ~800 tokens
- Meetings: ~600 tokens
- System + schema: ~500 tokens
- **Total input: ~4500-5500 tokens**
- **Output: ~800-1200 tokens**
- **Daily cost at Claude Sonnet 4: ~$0.015**

---

## Telegram Message Format (slim classification)

Rendered by `telegram_send.format_classification_message()`:

```
📊 *Daily Summary — {weekday} {date}*

{day_theme}

✅ *Done*
1. {task_name}                    # or (2/3×) for recurring target_count > 1
2. {task_name}

❌ *Unfinished*
3. {task_name} — {reason}

💡 *Unclassified Activities*      # full detail only here
4. {suggested_name} — {time_range} ({duration_minutes}m)
   {description}

🕐 {top 3 apps, one line}
💰 ${daily_cost:.4f}
```

Initial delivery footer:
```
_Reply to edit items, or say approve to save to Notion._
```

After an edit:
```
_Edit applied (v2). Reply to keep editing, or say approve to save._
```

### Approval replies
Exact-match phrases trigger final save (not substring — "Task 3 is done" is an edit):
`approve`, `approved`, `ok`, `looks good`, `good`, `yes`, `done`, `lgtm`, `confirm`,
`perfect`, `great`, `correct`, `👍`

All other replies while pending are passed to `pending_summary.apply_edit()` (Haiku).

### Plan edits (legacy)
When no pending summary exists, `telegram_webhook.py` still handles plan edits via
`plan_store.update_plan()`. See `plan_store.py` for tomorrow's plan schema.

---

## OpenClaw Integration (`claw.md`)

The `claw.md` file registers this system as an OpenClaw skill.
It wires the cron trigger, Telegram delivery, and reply handling.
See `claw.md` in root of this repo for the full skill definition.

**Routing priority:** if a pending summary exists (check via
`python delivery/telegram_webhook.py --has-pending`), route ALL non-`/` replies to
`--approve "{user_message}"` — the bot is in review mode until the user approves.

Key cron entry: `0 20 * * *` (8 PM daily)
Timezone: read from system or `config.yaml`

---

## Configuration (`config.yaml`)

```yaml
# Anthropic
anthropic_api_key: "sk-ant-..."
anthropic_model: "claude-sonnet-4-6"

# Notion
notion_api_key: "secret_..."
notion_meetings_db_id: "..."        # your meetings database ID
notion_context_page_id: "..."       # your "Agent Context" page ID

# Telegram
telegram_bot_token: "..."           # from @BotFather
telegram_chat_id: "..."             # your personal chat ID

# Paths
typing_log_dir: "~/typing-logs"
plans_dir: "~/.daily-agent/plans"
learning_dir: "~/.daily-agent/learning"
agent_log_dir: "~/daily-agent/logs"
pending_dir: "~/.daily-agent/pending"

# Classification
classification_done_threshold: 0.80
classification_min_threshold: 0.40

# Activity namer clustering
activity_cluster_gap_minutes: 5
activity_cluster_keyword_overlap: 2

# Self-learning
learning_few_shot_count: 5
learning_enabled: true

# Notion Tasks DB
notion_tasks_db_id: "..."
notion_projects_db_id: "..."

# ActivityWatch
activitywatch_host: "http://localhost:5600"
activitywatch_hostname: ""          # leave empty to auto-detect

# Pipeline
run_hour: 20                        # 8 PM
timezone: "Asia/Jerusalem"          # your timezone

# Debug trace (summarization_prompts/YYYY-MM-DD.md)
summarization_prompts_dir: "summarization_prompts"
debug_trace_enabled: true
debug_trace_max_list_items: 50
debug_trace_max_aw_events: 100
debug_trace_max_section_chars: 30000

# Filtering — apps to SKIP entirely from typing log
typing_ignore_bundle_ids:
  - "com.apple.keychainaccess"
  - "com.1password.1password"
  - "net.aggimenez.Proxyman"

# Filtering — apps where we want full text (others get char-count only)
typing_deep_apps:
  - bundle_id: "com.google.Chrome"
    domains: ["claude.ai", "chat.openai.com", "gemini.google.com", "aistudio.google.com"]
  - bundle_id: "com.todesktop.230313mzl4w4u92"   # Cursor
    mode: "full"
  - bundle_id: "com.apple.Notes"
    mode: "full"
  - bundle_id: "com.notion.mac"
    mode: "summary"                 # char-count only (content is in Notion already)
```

---

## Implementation Notes & Gotchas

### Debug trace (`summarization_prompts/`)
On every `run_daily.py` run (when `debug_trace_enabled: true`), `pipeline/daily_trace.py`
writes **`summarization_prompts/YYYY-MM-DD.md`** at repo root with truncated I/O for each
stage: collect → 1A presummary → 1B timeline → 1C classify → activity namer → narrate →
final result → Telegram preview. Re-runs on the same date overwrite the file.

Config keys: `summarization_prompts_dir`, `debug_trace_enabled`, `debug_trace_max_list_items`,
`debug_trace_max_aw_events`, `debug_trace_max_section_chars`.

```bash
python pipeline/run_daily.py --dry-run          # writes trace (unless disabled in config)
python pipeline/run_daily.py --dry-run --no-trace   # skip trace file
```

The trace file is gitignored (may contain sensitive typing/content).

### Per-entry raw data truncation
Every individual raw data item is hard-truncated to **20 lines** before being
included in any prompt. This applies to:
- Each typing entry's `text` field (from the typing daemon)
- Each Cursor chat turn (user messages fed to the pre-summarizer)

20 lines ≈ 80–120 tokens — enough to capture the gist of a message or coding
prompt without letting one long entry crowd out the rest of the day.

Implemented via `_truncate_entry()` in `pipeline/summarizer.py`. The cap is
controlled by the `_MAX_ENTRY_LINES` constant (default: 20). Truncated entries
get a `[... N more line(s) truncated]` suffix so the model knows content was cut.

This is a pre-processing step that runs *before* the per-source compression
(`_maybe_compress`, threshold: 8000 tokens) and the timeline token budget cuts.
Meetings are excluded — each meeting summary is already capped at 500 chars by
the prompt-building code.

### Typing log deduplication
The daemon may log the same text multiple times if the user edits and re-submits.
`collect_typing.py` must deduplicate by (app, text) within a 5-minute window.
Also filter entries with `len(text) < 10` — likely accidental keystrokes.

### ActivityWatch idle detection
Always intersect window events with the AFK bucket. A user "in Chrome for 3 hours"
may have been idle for 2 of those. Use:
```python
# Only count events where AFK status = "not-afk"
```

### Cursor workspace detection
Multiple workspace folders exist. Filter by `createdAt` timestamp matching today.
Workspaces with no activity today will have old timestamps — skip them.

### Notion API pagination
Meeting DB queries must handle pagination if >100 results (unlikely daily, but safe).

### Claude API error handling
Always catch `anthropic.APIError`. On failure, save raw collected data to
`~/.daily-agent/pending/YYYY-MM-DD.json` so it can be retried manually.

### Telegram message length
Telegram messages are capped at 4096 chars. If summary is longer, split into
two messages: summary first, then plan.

### Security
- `config.yaml` must be `chmod 600`
- Never log API keys or full Notion content to the agent log
- The `typing-ignore-bundle_ids` list must include password managers

---

## What Is Already Built

| Component | Status |
|---|---|
| Typing daemon | ✅ DONE — do not modify |
| Collectors (typing, AW, cursor, meetings, tasks) | ✅ DONE |
| Timeline builder + Cursor pre-summary (Stage 1A/1B) | ✅ DONE |
| Task classifier + confidence thresholds (Stage 1C) | ✅ DONE |
| Unclassified activity namer (clustering + naming) | ✅ DONE |
| Iterative approval loop (pending_summary + webhook) | ✅ DONE |
| Self-learning MVP (learning_store + few-shot injection) | ✅ DONE |
| Slim Telegram classification message | ✅ DONE |
| Legacy plan edit flow (plan_store) | ✅ DONE |
| OpenClaw wiring (claw.md) | ✅ DONE — update routing as needed |

## Testing

```bash
# Single collector
python collectors/collect_typing.py --date 2026-05-29 --dry-run
python collectors/collect_tasks.py --today

# Classifier (built-in fixture)
python pipeline/classifier.py --test

# Activity namer (built-in fixture)
python pipeline/activity_namer.py --test

# Full pipeline dry-run (prints slim Telegram message)
python pipeline/run_daily.py --dry-run

# Show classifier prompt including few-shot examples
python pipeline/run_daily.py --show-prompt

# Approval loop simulation
python -c "from pipeline import pending_summary; ..."
python delivery/telegram_webhook.py --approve "Draft Email is actually done"
python delivery/telegram_webhook.py --approve "looks good"
python delivery/telegram_webhook.py --has-pending   # exit 1 after approval

# Token / cost tracking
python utils/token_logger.py --today --type stage1_classify
python pipeline/run_daily.py --show-costs

# One-time: add Target Count + Recurrence to Tasks DB
python scripts/add_task_fields.py
```
