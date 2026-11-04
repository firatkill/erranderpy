"""Start the bot:  uv run python -m erranderpy   (./start.sh does this for you)."""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from erranderpy.assistant import Assistant
from erranderpy.config import load_settings
from erranderpy.core.db import Database
from erranderpy.core.llm import ConfigError, select_provider
from erranderpy.core.registry import AgentLoadError, load_agents
from erranderpy.gateway.telegram import build_application
from erranderpy.pairing import Pairing


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot
    # token. Keep those logs quiet so the token never lands in a terminal or log file.
    for noisy in ("httpx", "httpx2", "telegram.ext"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        settings = load_settings()
        llm, reason = select_provider()
        db = Database(settings.db_path)
        agents = load_agents(db=db)
    except (ConfigError, AgentLoadError) as exc:
        print(f"✗ {exc}\n  Fix .env (or run `make doctor`) and start again.", file=sys.stderr)
        return 1

    print(f"✓ {reason}")
    print(f"✓ Agents: {', '.join(agents) or 'none'}")
    print(f"✓ Database: {settings.db_path}")

    pairing = Pairing(db)
    if pairing.code:
        print()
        print(f"🔑 Pairing code: {pairing.code}")
        print("   Open your bot on Telegram and send it this code. Whoever sends it becomes the owner.")
        print()
    else:
        print("✓ Paired with your Telegram account")

    assistant = Assistant(llm, agents, db, settings.timezone, settings.language)
    app = build_application(settings.telegram_token, assistant, pairing, settings.language)
    print("✅ Ready. Write to your bot on Telegram. (Ctrl+C to stop)")
    app.run_polling(allowed_updates=["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
