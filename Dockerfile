# Optional way to run the bot (Yol B in SPEC §12). The default is ./start.sh + make service.
#   cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN and one LLM key
#   docker compose up -d && docker compose logs -f   # the pairing code is in the logs
FROM python:3.12-slim

# uv from its official image, pinned: the same tool start.sh uses.
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, in their own layer: code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY erranderpy ./erranderpy
COPY agents ./agents
# The on-call canary runs the router eval set, so it ships with the bot.
COPY evals ./evals
RUN uv sync --frozen --no-dev

# data/ (database, abilities the bot creates, logs) is a volume: see docker-compose.yml.
# Runs as root inside the container on purpose: the mounted ./data folder then works
# whoever owns it on the host (often root on a VPS). The bot opens no ports.
CMD ["/app/.venv/bin/python", "-m", "erranderpy"]
