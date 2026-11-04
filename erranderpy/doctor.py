"""`make doctor`: check the setup and say exactly what is wrong.

Each check is a function returning (ok, message). The setup wizard reuses
`check_telegram` and `check_llm` to validate what the user types.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from erranderpy.config import ROOT, load_settings
from erranderpy.core.db import Database
from erranderpy.core.llm import ConfigError, LLMError, LLMProvider, Message, select_provider
from erranderpy.core.registry import AgentLoadError, load_agents
from erranderpy.pairing import Pairing

Check = tuple[bool, str]


def check_telegram(token: str) -> Check:
    try:
        # The token is part of the URL by Telegram's design; we never log or print it.
        response = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    except httpx.HTTPError as exc:
        return False, f"Telegram: can't reach api.telegram.org ({type(exc).__name__})"
    if response.status_code == 401 or response.status_code == 404:
        return False, "Telegram: the bot token was rejected. Copy it again from @BotFather."
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        return False, f"Telegram: unexpected answer (HTTP {response.status_code})"
    return True, f"Telegram: bot @{data['result']['username']}"


def check_llm(provider: LLMProvider) -> Check:
    try:
        result = provider.complete("Reply with the single word OK.", [Message.user("ping")], max_tokens=256)
    except LLMError as exc:
        hint = " The API key was rejected." if exc.kind == "auth" else ""
        return False, f"LLM {provider.name}/{provider.model}: {exc.kind}.{hint}"
    return True, f"LLM: {provider.name} / {provider.model} answered in {result.latency_ms / 1000:.1f}s"


def run() -> int:
    results: list[Check] = []
    env_file = ROOT / ".env"
    if not env_file.exists():
        print("✗ .env not found. Run ./start.sh (it creates one) or copy .env.example to .env.")
        return 1
    load_dotenv(env_file)

    try:
        settings = load_settings()
        results.append((True, f"Settings: language={settings.language}, timezone={settings.timezone.key}"))
        results.append(check_telegram(settings.telegram_token))
    except ConfigError as exc:
        results.append((False, str(exc)))
        settings = None

    try:
        provider, reason = select_provider()
        results.append((True, reason))
        results.append(check_llm(provider))
    except ConfigError as exc:
        results.append((False, str(exc)))

    try:
        agents = load_agents()
        results.append((True, f"Agents: {', '.join(agents) or 'none'}"))
    except AgentLoadError as exc:
        results.append((False, f"Agents: {exc}"))

    if settings is not None:
        try:
            db = Database(settings.db_path)
            pairing = Pairing(db)
            state = "paired" if pairing.paired else "not paired yet (start the bot to get a code)"
            results.append((True, f"Database: {_relative(settings.db_path)}, {state}"))
        except Exception as exc:  # disk full, permissions, ...
            results.append((False, f"Database: {exc}"))

    for ok, message in results:
        print(("✓ " if ok else "✗ ") + message)
    failed = sum(not ok for ok, _ in results)
    print("\nAll good." if not failed else f"\n{failed} problem(s) found.")
    return 1 if failed else 0


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    sys.exit(run())
