#!/usr/bin/env bash
# Start erranderpy. First run: installs everything and asks for your keys.
# Later runs: starts the bot right away.
set -euo pipefail
cd "$(dirname "$0")"

# 1) uv manages Python and the packages, so nothing else needs to be installed.
if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  else
    echo "erranderpy uses uv (https://docs.astral.sh/uv/) to install Python and its packages."
    echo "uv is not installed. It can be installed with its official script: https://astral.sh/uv/install.sh"
    read -r -p "Install uv now? [Y/n] " answer || answer="Y"
    case "${answer:-Y}" in
      [Nn]*) echo "Install uv yourself, then run ./start.sh again."; exit 1 ;;
    esac
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

# 2) Dependencies (the first run also downloads the right Python version).
echo "→ Installing dependencies..."
uv sync --quiet

# 3) First run: the setup wizard writes .env.
if [ ! -f .env ]; then
  uv run --quiet python -m erranderpy.wizard
fi

# 4) Start the bot (prints a pairing code the first time).
exec uv run --quiet python -m erranderpy
