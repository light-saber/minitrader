# MiniTrader Makefile
# Common commands for setup, demos, and tests.
# Always use the venv Python — never the system Python.

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := uv pip install --python $(PY)

.PHONY: help install demo status digest test clean

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:  ## Create venv + install dependencies
	uv venv $(VENV) --python 3.11
	$(PIP) -r requirements.txt
	@echo "✓ MiniTrader ready. Try: make demo"

demo:  ## Render a demo chart pair (default: INFY). Usage: make demo SYM=RELIANCE
	$(PY) chart_render.py --demo $(or $(SYM),INFY)

status:  ## Show live + paper portfolio status
	$(PY) subagent.py status

digest:  ## Run the daily digest in dry-run mode (prints, no Discord post)
	$(PY) daily_digest.py --dry-run

earnings:  ## Print the 5-day earnings blackout set
	$(PY) earnings_calendar.py --dry-run

test:  ## Run import + smoke checks on all modules
	@for f in *.py; do $(PY) -m py_compile $$f && echo "  ✓ $$f"; done
	@echo "✓ all modules compile"
	@$(PY) subagent.py status

clean:  ## Remove venv + generated charts + pycache (keeps state.json + trades.csv)
	rm -rf $(VENV) __pycache__ charts/png/*.png charts/html/*.html logs/*.log
