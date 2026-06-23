# Daily Agent

Personal Mac productivity agent. Collects typing/apps/Cursor/Notion daily, classifies
against Notion Tasks DB via Claude, delivers slim Telegram summary. User approves →
tasks marked done in Notion. Everything local except Claude API (~$0.03/day).

## Commands

```bash
source .venv/bin/activate   # always first

make check                  # health check (deps + services)
make test                   # pytest tests/ -v  (25 tests, all mocked)
make run                    # dry-run: collect + classify + print, no send
make send                   # full run + Telegram

python pipeline/run_daily.py --date 2026-06-14 --dry-run   # specific date
python pipeline/run_daily.py --collect-only                 # dump raw JSON
python pipeline/run_daily.py --show-prompt                  # print classifier prompt
python pipeline/run_daily.py --show-costs --days 7

python pipeline/classifier.py --test        # standalone fixture test
python pipeline/activity_namer.py --test

python delivery/telegram_webhook.py --poll              # start reply listener
python delivery/telegram_webhook.py --approve "ok"      # simulate approval
python delivery/telegram_webhook.py --message "move 3 to next week"

python collectors/collect_tasks.py --today
python health_check.py --skip-notion --skip-telegram
```

## Architecture

```
Stage 1A  Haiku — cursor pre-summary (per session)
Stage 1B  Python — build timeline (AW + typing + cursor)
Stage 1C  Sonnet — classify segments → Notion tasks
          Haiku — name unclassified clusters
Stage 2   Sonnet — narrate day_theme (done tasks only)
Stage 3   save pending-YYYY-MM-DD.json (awaiting_approval)
Stage 4   send Telegram (MarkdownV2, single message)

On approval reply → mark tasks Done in Notion + write daily entry
```

## Key Files

| File | Role |
|---|---|
| `pipeline/run_daily.py` | Main orchestrator |
| `pipeline/classifier.py` | Task classification + `post_process()` |
| `pipeline/summarizer.py` | Timeline builder + `narrate()` |
| `pipeline/pending_summary.py` | Approval state machine |
| `delivery/telegram_send.py` | MarkdownV2 formatting + send |
| `delivery/telegram_webhook.py` | Reply handler (`--poll` mode) |
| `collectors/collect_tasks.py` | Notion Tasks/Projects DB |
| `config.yaml` | All config (chmod 600) |
| `health_check.py` | Pre-flight dependency check |

## Background Services

Two launchd agents must always be running:

```bash
launchctl list com.user.typing-capture       # keystroke capture daemon
launchctl list com.user.daily-agent-webhook  # Telegram long-poll listener

launchctl start com.user.typing-capture
launchctl start com.user.daily-agent-webhook
```

Plist location: `~/Library/LaunchAgents/`. If approval replies are silently
ignored, the webhook daemon is down.

## Gotchas

**Typing log path**: daemon writes to `~/typing-logs/` (no dot, with s).
Config key `typing_log_dir: "~/typing-logs"`. Old path `~/.typing-log` is wrong.

**notion-client**: pin `>=2.1.0,<2.6`. v2.6+ removed `databases.query`.

**Task filter**: `get_classifiable_tasks()` queries Status ∈ Today | In-Progress |
This Week. No Scheduled Date filter — tasks are included regardless of date field.

**Classifier scoring**: each task scored independently against the full timeline.
Multiple tasks may cite the same segment. `unmatched_segments` computed in Python
(`post_process()`), not by the LLM.

**Per-entry truncation**: every typing entry and Cursor turn hard-capped at 20
lines before entering any prompt (`_truncate_entry()` in `summarizer.py`).

**Notion writes deferred**: nothing written to Notion until Telegram approval.
Pending state lives in `plans_dir/pending-YYYY-MM-DD.json`.

**Telegram format**: MarkdownV2 — all special chars must be backslash-escaped.
Done tasks show `||spoiler||` evidence (tap to expand). Single message, no split.

**anthropic_max_tokens**: set to 2500 in config. 1500 causes truncated JSON.
