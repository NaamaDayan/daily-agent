# Daily Agent — convenience targets
#
# Usage:
#   make setup    install Python dependencies
#   make check    run health_check.py (verifies all external services)
#   make test     run pytest test suite
#   make run      dry-run the pipeline for today (no Telegram, no save)
#   make send     run the pipeline and send to Telegram
#   make plan     show tomorrow's plan
#   make micro    run the 30-min micro-summarizer for the current window
#   make clean    remove Python bytecode and test cache

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest

.PHONY: setup check test run send plan micro clean help

# ── Core targets ──────────────────────────────────────────────────────────────

setup:
	$(PYTHON) -m pip install -r requirements.txt

check:
	$(PYTHON) health_check.py

test:
	$(PYTEST) tests/ -v

run:
	$(PYTHON) pipeline/run_daily.py --dry-run

send:
	$(PYTHON) pipeline/run_daily.py --send

plan:
	$(PYTHON) pipeline/plan_store.py --show

micro:
	$(PYTHON) pipeline/micro_summarizer.py --run

# ── Additional useful targets ─────────────────────────────────────────────────

collect:
	@# Run collectors only and print JSON (useful for debugging)
	$(PYTHON) pipeline/run_daily.py --collect-only

telegram-test:
	@# Send a test Telegram message to verify bot token + chat_id
	$(PYTHON) delivery/telegram_send.py --test

telegram-preview:
	@# Preview the formatted summary message without sending
	$(PYTHON) delivery/telegram_send.py --preview

init-notion:
	@# Create the General Context sub-page in Notion (one-time setup)
	$(PYTHON) context/init_context_page.py

micro-show:
	@# Show today's micro-summaries
	$(PYTHON) pipeline/micro_summarizer.py --show

plan-list:
	@# List all saved plan files
	$(PYTHON) pipeline/plan_store.py --list

# ── Maintenance ────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -not -path "./.venv/*" -delete 2>/dev/null || true
	rm -rf .pytest_cache

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  Daily Agent — make targets"
	@echo ""
	@echo "  Core:"
	@echo "    make setup          pip install -r requirements.txt"
	@echo "    make check          python health_check.py"
	@echo "    make test           pytest tests/ -v"
	@echo "    make run            pipeline --dry-run (no Telegram)"
	@echo "    make send           pipeline --send (full run)"
	@echo "    make plan           show tomorrow's plan"
	@echo "    make micro          run micro-summarizer for now"
	@echo ""
	@echo "  Setup:"
	@echo "    make init-notion    create General Context page in Notion"
	@echo "    make telegram-test  send test message to verify bot"
	@echo ""
	@echo "  Debug:"
	@echo "    make collect        run collectors only, print JSON"
	@echo "    make telegram-preview  print formatted message without sending"
	@echo "    make micro-show     show today's micro-summaries"
	@echo "    make plan-list      list all saved plan files"
	@echo ""
	@echo "  Maintenance:"
	@echo "    make clean          remove __pycache__ and .pytest_cache"
	@echo ""
