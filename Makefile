.PHONY: help install test test-live eval compare

help:  ## Show the available commands
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-10s %s\n", $$1, $$2}'

install:  ## Install Python and dependencies (uv)
	uv sync

test:  ## Run the offline tests (no API calls, free)
	uv run pytest

test-live:  ## Run contract tests against every provider that has a key in .env (costs < $0.01)
	uv run pytest -m live -v

eval:  ## Run the eval set with the provider the bot would use
	uv run python -m erranderpy.evals

compare:  ## Run the eval set with every provider that has a key and compare them
	uv run python -m erranderpy.evals --all
