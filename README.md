# Daily Agent

A personal productivity agent that runs silently on your Mac. Every evening at
20:00 it collects everything you actually did — typed text, active apps via
ActivityWatch, Cursor IDE sessions, and Notion meeting summaries — synthesizes
a semantic daily summary and prioritized next-day plan via the Claude API, and
delivers it to your personal Telegram chat. The whole pipeline takes ~~20 seconds
and costs roughly $0.015 per run (~~$5/year).

You reply to the Telegram message to edit your plan: "Move 3 to next week",
"Add: review investor deck as priority 1". The agent applies your edit via a
small Claude call and sends back the updated plan. Optionally, a micro-summarizer
cron runs every 30 minutes during the day to build incremental summaries,
cutting end-of-day token usage by ~60%. All logic runs locally; the only
outbound call is one Claude API request per day.

---

## Prerequisites


| Dependency                | Why                                                                                            | Install                                                                                                                  |
| ------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **ActivityWatch**         | Tracks active app + window title at 1-second resolution; provides the time-allocation backbone | [activitywatch.net](https://activitywatch.net)                                                                           |
| **Cursor IDE**            | Chat sessions are read from its SQLite workspace storage                                       | [cursor.sh](https://cursor.sh) — already installed if you use it                                                         |
| **Typing daemon**         | Logs what you type per app; runs as a macOS background service                                 | `typing-capture/` in this repo — see setup step 4                                                                        |
| **Notion**                | Source of meeting summaries and general context page                                           | [notion.so](https://notion.so) + create an integration at [notion.so/my-integrations](https://notion.so/my-integrations) |
| **Telegram bot**          | Receives the daily summary and plan-edit replies                                               | Create via [@BotFather](https://t.me/BotFather)                                                                          |
| **Anthropic API key**     | Powers the summarizer and plan-edit calls                                                      | [console.anthropic.com](https://console.anthropic.com)                                                                   |
| **Python 3.11+**          | Runtime                                                                                        | `brew install python`                                                                                                    |
| **OpenClaw** *(optional)* | Manages the 20:00 cron trigger and Telegram message routing                                    | See [claw.md](claw.md)                                                                                                   |


---

## Setup

### 1. Clone and install dependencies

```bash
git clone <your-repo-url> ~/daily-agent
cd ~/daily-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Fill in config.yaml

```bash
cp config.yaml.example config.yaml   # or edit config.yaml directly
nano config.yaml                      # fill in all required values
chmod 600 config.yaml                 # protect API keys
```

Required values to fill in:

- `anthropic_api_key` — from [console.anthropic.com](https://console.anthropic.com)
- `notion_api_key` — from [notion.so/my-integrations](https://notion.so/my-integrations) → create integration
- `notion_meetings_db_id` — 32-char ID from your meetings database URL
- `notion_context_page_id` — 32-char ID from your agent context page URL
- `telegram_bot_token` — from [@BotFather](https://t.me/BotFather) → /newbot
- `telegram_chat_id` — send `/start` to [@userinfobot](https://t.me/userinfobot)
- `timezone` — your local timezone (e.g. `America/New_York`, `Asia/Jerusalem`)

### 3. Share Notion pages and databases with your integration

In Notion, open **each** of the following → `···` menu → **Connections** → add your integration (`transcript_automation` or whatever you named it):


| Object                                        | URL hint                 |
| --------------------------------------------- | ------------------------ |
| `Daily Plans - Plan VS Actual` (context page) | `notion_context_page_id` |
| Research / Meetings DB                        | `notion_meetings_db_id`  |
| **Tasks DB**                                  | `notion_tasks_db_id`     |
| **Projects DB**                               | `notion_projects_db_id`  |


> **Important:** Notion does NOT automatically propagate integration access from a parent page to full-page child databases. You must share each database separately, even if the parent page is already shared. Run `python health_check.py` after sharing — all 4 Notion objects should show ✅.

### 4. Start the typing daemon (one-time)

```bash
cd typing-capture
```

To have it start at login, add a launchd plist or use the provided script.

### 5. Create the Notion context sub-page

```bash
python3 context/init_context_page.py
# Opens a URL → click it, fill in your background context in Notion
```

### 6. Run the health check

```bash
python3 health_check.py
```

All 8 items should show ✅. Fix any ❌ before proceeding.

### 7. First dry run

```bash
python3 pipeline/run_daily.py --dry-run
```

Review the output. If the summary looks reasonable, you're ready to go live.

### 8. Enable the cron

```bash
# Option A — standard cron (no OpenClaw)
crontab -e
# Add: 0 20 * * * cd ~/daily-agent && python3 pipeline/run_daily.py >> logs/agent.log 2>&1

# Option B — OpenClaw (see §How to enable via OpenClaw below)
```

---

## config.yaml Field Reference

### Anthropic


| Key                                      | Type   | Description                                                                         |
| ---------------------------------------- | ------ | ----------------------------------------------------------------------------------- |
| `anthropic_api_key`                      | string | **Required.** `sk-ant-...` key from console.anthropic.com                           |
| `anthropic_model`                        | string | Main model for daily summary. Default: `claude-sonnet-4-6`                          |
| `anthropic_max_tokens`                   | int    | Max output tokens for the main summary call. Default: `1500`                        |
| `anthropic_cursor_presummary_model`      | string | Cheaper model for Stage 1A Cursor pre-summary. Default: `claude-haiku-4-5-20251001` |
| `anthropic_cursor_presummary_max_tokens` | int    | Max output per Cursor session pre-summary. Default: `200`                           |


### Notion


| Key                                 | Type   | Description                                                                                                     |
| ----------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------- |
| `notion_api_key`                    | string | **Required.** `ntn_...` integration secret                                                                      |
| `notion_meetings_db_id`             | string | **Required.** 32-char ID of your meetings database                                                              |
| `notion_context_page_id`            | string | **Required.** 32-char ID of your agent context page                                                             |
| `notion_meetings_type_filter`       | string | Value of the "Type" property used to filter meeting pages. Default: `Conversation`                              |
| `notion_sikhum_heading`             | string | Heading in meeting pages marking the summary section (Hebrew: `סיכום`). Change if your headings are in English. |
| `notion_transcript_heading`         | string | Heading marking the raw transcript — content after this is excluded.                                            |
| `notion_general_context_page_title` | string | Title of the "General Context" sub-page. Default: `General Context`                                             |


### Telegram


| Key                  | Type   | Description                                                                  |
| -------------------- | ------ | ---------------------------------------------------------------------------- |
| `telegram_bot_token` | string | **Required.** Token from @BotFather                                          |
| `telegram_chat_id`   | string | **Required.** Your personal chat ID (from @userinfobot)                      |
| `telegram_max_chars` | int    | Split messages longer than this. Default: `4000` (Telegram hard limit: 4096) |


### Paths


| Key              | Type | Description                                                                                     |
| ---------------- | ---- | ----------------------------------------------------------------------------------------------- |
| `typing_log_dir` | path | Where the typing daemon writes daily JSON files. Default: `~/.typing-log`                       |
| `plans_dir`      | path | Where tomorrow's plan JSON files are saved. Default: `~/.daily-agent/plans`                     |
| `agent_log_dir`  | path | Where pipeline log files are written. Default: `~/daily-agent/logs`                             |
| `pending_dir`    | path | Where raw data is saved on pipeline failure for manual retry. Default: `~/.daily-agent/pending` |


### ActivityWatch


| Key                      | Type   | Description                                                                   |
| ------------------------ | ------ | ----------------------------------------------------------------------------- |
| `activitywatch_host`     | string | AW REST API base URL. Default: `http://localhost:5600`                        |
| `activitywatch_hostname` | string | Override the hostname used to look up AW buckets. Leave empty to auto-detect. |


### Schedule


| Key        | Type   | Description                                                                                                      |
| ---------- | ------ | ---------------------------------------------------------------------------------------------------------------- |
| `run_hour` | int    | Hour (24h) the pipeline runs. Default: `20` (8 PM). Used for documentation; actual trigger is via cron/OpenClaw. |
| `timezone` | string | pytz-compatible timezone string. Used to convert UTC timestamps to local time in the summary.                    |


### Typing Log Filtering


| Key                           | Type | Description                                                                                                                        |
| ----------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `typing_ignore_bundle_ids`    | list | Bundle IDs to skip entirely (password managers, terminals).                                                                        |
| `typing_min_length`           | int  | Minimum text length to keep (filters accidental keystrokes). Default: `15`                                                         |
| `typing_dedup_window_seconds` | int  | Same app + same text within this window = deduplicated. Default: `300`                                                             |
| `typing_deep_apps`            | list | Apps with full/domain-filtered text capture. Each entry: `{bundle_id, mode, domains?}`. Modes: `full`, `domain_filter`, `summary`. |
| `typing_summary_only_apps`    | list | Bundle IDs that get char-count only (content already in Notion, etc.).                                                             |


### Token Budget


| Key                      | Type | Description                                                                         |
| ------------------------ | ---- | ----------------------------------------------------------------------------------- |
| `min_segment_minutes`    | int  | AW segments shorter than this (minutes) are dropped from the timeline. Default: `3` |
| `max_timeline_tokens`    | int  | Hard cap on the timeline section of the main prompt. Default: `3000`                |
| `max_typing_per_segment` | int  | Maximum typing entries included per timeline segment. Default: `6`                  |
| `max_typing_tokens`      | int  | Truncate typing section if over this. Default: `2500`                               |
| `max_cursor_tokens`      | int  | Truncate Cursor sessions if over this. Default: `1000`                              |
| `max_meeting_tokens`     | int  | Truncate meeting summaries if over this. Default: `800`                             |
| `max_context_tokens`     | int  | Truncate context page if over this. Default: `600`                                  |


---

## Running Manually

```bash
# Full run for today — collects, summarizes, saves plan, sends Telegram
python3 pipeline/run_daily.py

# Dry run — collect + summarize + print to terminal, no save, no Telegram
python3 pipeline/run_daily.py --dry-run

# Run for a past date and re-send
python3 pipeline/run_daily.py --date 2026-05-28 --send

# Collectors only — print raw JSON, exit
python3 pipeline/run_daily.py --collect-only

# Show / edit today's plan
python3 pipeline/plan_store.py --show
python3 pipeline/plan_store.py --edit "move 3 to next week"
python3 pipeline/plan_store.py --done 2

# Run micro-summarizer manually (summarizes last 30 min)
python3 pipeline/micro_summarizer.py --run
python3 pipeline/micro_summarizer.py --show   # show today's micro-summaries

# Test Telegram connectivity
python3 delivery/telegram_send.py --test
python3 delivery/telegram_send.py --preview   # print formatted message, no send

# Simulate a Telegram reply
python3 delivery/telegram_webhook.py --message "move 3 to next week"

# Re-run after a failure (raw data saved to pending/)
python3 pipeline/run_daily.py --date $(ls ~/.daily-agent/pending/*.json | head -1 | xargs basename | cut -d. -f1)
```

---

## How to Enable via OpenClaw

OpenClaw reads `claw.md` to register this as a skill with a cron trigger and
Telegram message routing.

1. Copy `claw.md` to your OpenClaw skills directory (or symlink it).
2. In `claw.md`, the cron is already set to `0 20 * * *`. Confirm the
  `timezone` field matches `config.yaml`.
3. OpenClaw will execute `python3 pipeline/run_daily.py` at 20:00 daily.
4. To handle Telegram replies, run the long-poll listener as a background process:
  ```bash
   python3 delivery/telegram_webhook.py --poll &
  ```
   Or let OpenClaw route incoming messages by matching the trigger words in `claw.md`.

For micro-summaries, add a separate cron:

```cron
*/30 9-20 * * *  cd ~/daily-agent && python3 pipeline/micro_summarizer.py --run >> logs/micro.log 2>&1
```

---

## Troubleshooting

### 1. `❌ ActivityWatch — Connection refused`

**Cause:** ActivityWatch is not running.

**Fix:**

```bash
open -a ActivityWatch   # start the macOS app
# verify: curl http://localhost:5600/api/0/info
```

If AW was never installed: download from [activitywatch.net](https://activitywatch.net).

---

### 2. `❌ Notion pages/DBs — context page 36f9c344… not found (404)`

or `403 not shared with integration`

**Cause:** Your Notion integration doesn't have access to the page.

**Fix:** In Notion, open the affected page → `···` menu → **Connections** → add
your integration. Then re-run `python3 health_check.py`.

---

### 3. Telegram message shows `❌ Markdown parse error`

**Cause:** A task name or highlight contains a `*` or `_` character that
confuses Telegram's Markdown v1 parser.

**Fix:** The delivery code already falls back to plain text automatically.
To prevent it, avoid bare asterisks/underscores in task names. The `_send_md`
function in `delivery/telegram_send.py` handles this transparently.

---

### 4. Cursor sessions show 0 entries

**Cause:** The workspace SQLite path doesn't match, or the Cursor version
uses a different schema.

**Fix:**

```bash
python3 pipeline/run_daily.py --collect-only 2>&1 | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d['cursor'])"
```

If empty, check the SQLite paths in `collectors/collect_cursor.py`. The
expected locations are `~/.cursor/User/workspaceStorage/*/backup.db` and
`~/.cursor/User/workspaceStorage/*/state.vscdb`. The path may differ on
older Cursor versions — adjust `_WORKSPACE_ROOT` in that file.

---

### 5. Summary pipeline fails with exit code 1 / `pending/` file created

**Cause:** Claude returned non-JSON (network error, model refusal, rate limit).

**Fix:**

```bash
# See what was saved
cat ~/.daily-agent/pending/YYYY-MM-DD.json | python3 -m json.tool | head -40

# Retry after checking your API key / quota:
python3 pipeline/run_daily.py --date YYYY-MM-DD --dry-run
```

Check [status.anthropic.com](https://status.anthropic.com) for outages. If the
error is a JSON parse failure rather than an API error, the raw response is
logged in `logs/agent-YYYY-MM-DD.log`.

---

## Project Structure

```
daily-agent/
├── collectors/
│   ├── collect_typing.py          reads ~/.typing-log/ daemon output
│   ├── collect_activitywatch.py   queries ActivityWatch REST API
│   ├── collect_cursor.py          reads Cursor SQLite workspace storage
│   └── collect_notion_meetings.py fetches today's meeting summaries from Notion
├── pipeline/
│   ├── run_daily.py               main orchestrator (cron entry point)
│   ├── summarizer.py              two-stage Claude pipeline (Stage 1A+1B+2)
│   ├── micro_summarizer.py        optional 30-min incremental summaries
│   └── plan_store.py              persists + edits tomorrow's plan
├── delivery/
│   ├── telegram_send.py           sends formatted Markdown message via Bot API
│   └── telegram_webhook.py        receives reply, routes intents, applies edits
├── context/
│   ├── fetch_context.py           reads Notion context page + today's entry
│   ├── update_context.py          writes plan + actual back to Notion
│   └── init_context_page.py       one-time setup: creates General Context sub-page
├── tests/
│   ├── fixture_day.json           realistic test fixture (2026-05-29)
│   └── test_e2e.py                25 integration tests (all mocked, no API calls)
├── health_check.py                pre-flight check for all 8 dependencies
├── config.yaml                    all configuration (chmod 600)
├── config_loader.py               YAML loader with lru_cache + placeholder check
├── claw.md                        OpenClaw skill definition
├── Makefile                       convenience targets
└── requirements.txt
```

## Cost


| Component                  | Cost                                                              |
| -------------------------- | ----------------------------------------------------------------- |
| Main daily summary         | ~$0.015/day (claude-sonnet-4-6, ~1000 input + 1000 output tokens) |
| Micro-summarizer (48×/day) | ~$0.002/day (haiku, 150 tokens × 48 = 7 200 tokens)               |
| Plan edits                 | ~$0.001/edit                                                      |
| **Total**                  | **~$6/year**                                                      |


All other components (ActivityWatch, Cursor, Telegram, Notion free tier) are free.