"""Command line entry point for evals.

    uv run python -m erranderpy.evals              # the provider the bot would use
    uv run python -m erranderpy.evals --all        # every provider with a key in .env
    uv run python -m erranderpy.evals --providers deepseek,openai --suite router --runs 3

The Makefile wraps these as `make eval` and `make compare`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from erranderpy.core.llm import ConfigError, available_providers, build_provider, select_provider
from erranderpy.evals.report import markdown_report, summarize, summary_table
from erranderpy.evals.runner import EVALS_DIR, run_suite

SUITES = ["router", "tools"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erranderpy.evals", description="Run the Turkish eval set.")
    parser.add_argument("--all", action="store_true", help="run every provider that has an API key")
    parser.add_argument("--providers", help="comma-separated provider names, e.g. deepseek,openai")
    parser.add_argument("--suite", choices=[*SUITES, "all"], default="all")
    parser.add_argument("--runs", type=int, default=1, help="repeat each case N times (models are not deterministic)")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        if args.providers:
            providers = [build_provider(name.strip()) for name in args.providers.split(",")]
        elif args.all:
            names = available_providers()
            if not names:
                raise ConfigError("No LLM API key found in .env.")
            providers = [build_provider(name) for name in names]
        else:
            provider, reason = select_provider()
            print(reason)
            providers = [provider]
    except ConfigError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    suites = SUITES if args.suite == "all" else [args.suite]
    runs = []
    for provider in providers:
        label = f"{provider.name} / {provider.model}"
        print(f"→ {label} ...", flush=True)
        results = [r for suite in suites for r in run_suite(provider, suite, args.runs)]
        summary = summarize(label, results)
        runs.append((summary, results))

    print()
    print(summary_table([s for s, _ in runs]))

    reports_dir = EVALS_DIR.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    path = reports_dir / f"eval-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(markdown_report(runs, suites, args.runs), encoding="utf-8")
    print(f"\nFull report (with every failed case): {path.relative_to(EVALS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
