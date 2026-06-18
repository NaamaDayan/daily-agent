# Daily Agent — Prompt Suite

## Files in this package

| File | Purpose |
|---|---|
| `CLAUDE.md` | Full architecture document — Claude Code reads this first |
| `PROMPT_SUITE.md` | Ordered prompts to paste into Claude Code |
| `claw.md` | OpenClaw skill definition — copy to your daily-agent/ root |
| `config.yaml` | Configuration template — fill in your keys |
| `HOW_TO_USE.md` | This file |

## How to use

### Step 1 — Set up the project directory

```bash
mkdir ~/daily-agent
cd ~/daily-agent
```

Copy these four files into `~/daily-agent/`:
- `CLAUDE.md`
- `claw.md`
- `config.yaml`

### Step 2 — Fill in config.yaml

```bash
cp config.yaml ~/daily-agent/config.yaml
nano ~/daily-agent/config.yaml   # fill all REPLACE_ME values
chmod 600 ~/daily-agent/config.yaml
```

Required before starting:
- Anthropic API key (console.anthropic.com)
- Notion integration key + DB/page IDs (notion.so/my-integrations)
- Telegram bot token (from @BotFather) + your chat ID (from @userinfobot)

### Step 3 — Open Claude Code

```bash
cd ~/daily-agent
claude   # opens Claude Code in this directory
```

### Step 4 — Paste prompts in order

Open `PROMPT_SUITE.md` and paste each PROMPT_N block into Claude Code.
Wait for confirmation after each one before pasting the next.

Start with PROMPT_0 (bootstrap), then 1 through 10 in order.

**Do not skip prompts.** Each one builds on the previous.

### Step 5 — Run health check

```bash
cd ~/daily-agent
python health_check.py
```

All items should show ✅. Fix any ❌ before proceeding.

### Step 6 — Test dry run

```bash
python pipeline/run_daily.py --dry-run
```

Review the output. If it looks right, enable the cron via OpenClaw.

---

## What gets built

```
collectors/
  collect_typing.py        reads your ~/ .typing-log daemon output
  collect_activitywatch.py queries ActivityWatch for app time + URLs  
  collect_cursor.py        reads Cursor IDE chat sessions from SQLite
  collect_notion_meetings.py  fetches today's meeting summaries from Notion

pipeline/
  run_daily.py             main orchestrator (cron entry point)
  summarizer.py            builds prompt + calls Claude API
  micro_summarizer.py      optional 30-min incremental summaries
  plan_store.py            persists + edits tomorrow's plan

delivery/
  telegram_send.py         sends formatted message via Telegram Bot API
  telegram_webhook.py      receives reply, parses plan edits

context/
  fetch_context.py         reads your "Agent Context" Notion page
  update_context.py        appends context updates from Telegram

health_check.py            pre-flight check for all dependencies
claw.md                    OpenClaw skill (cron + Telegram routing)
config.yaml                all configuration
```

## Cost estimate

~$5/year in Anthropic API tokens for daily runs.
All other components are free and local.
