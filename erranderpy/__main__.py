"""Start the bot:  uv run python -m erranderpy   (./start.sh does this for you)."""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

from erranderpy.assistant import Assistant
from erranderpy.config import load_settings
from erranderpy.core.db import Database
from erranderpy.core.jobs import DISABLED
from erranderpy.core.llm import ConfigError, select_provider
from erranderpy.core.registry import AGENTS_DIR, AgentLoadError, load_agents
from erranderpy.config import ROOT
from erranderpy.gateway.telegram import build_application
from erranderpy.oncall.service import Oncall, note_start, note_stop
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
        agent_dirs = [AGENTS_DIR, settings.user_agents_dir]
        agents = load_agents(agent_dirs, db)
    except (ConfigError, AgentLoadError) as exc:
        print(f"✗ {exc}\n  Fix .env (or run `make doctor`) and start again.", file=sys.stderr)
        return 1

    print(f"✓ {reason}")
    print(f"✓ Agents: {', '.join(agents) or 'none'}  (yours: {settings.user_agents_dir})")
    for agent in agents.values():
        for job in agent.jobs:
            at = job.time_of_day()
            print(f"  ⏰ {agent.name}.{job.name}: " + (f"every day at {at:%H:%M}" if at else "off"))
    print(f"✓ Database: {settings.db_path}")

    pairing = Pairing(db)
    if pairing.code:
        print()
        print(f"🔑 Pairing code: {pairing.code}")
        print("   Open your bot on Telegram and send it this code. Whoever sends it becomes the owner.")
        print()
    else:
        print("✓ Paired with your Telegram account")

    assistant = Assistant(llm, agents, db, settings.timezone, settings.language, agent_dirs)
    oncall = Oncall(llm, db, lambda: assistant.agents, settings.language, repo_root=ROOT)
    report_at = os.environ.get("ONCALL_REPORT_TIME", "").strip() or "08:00"
    print(f"✓ On-call: checks every 5 min, report " + ("off" if report_at.lower() in DISABLED else f"every day at {report_at}"))
    unclean = note_start(db)
    if unclean:
        print("⚠️ The previous run did not stop cleanly (crash, kill or power cut); telling the owner.")
    app = build_application(
        settings.telegram_token, assistant, pairing, settings.language, settings.timezone, oncall, report_at,
        startup_notice=oncall.restart_notice() if unclean else None,
    )
    print("✅ Ready. Write to your bot on Telegram. (Ctrl+C to stop)")
    # Returns normally on Ctrl+C and on SIGTERM (systemctl stop, launchctl bootout): a clean
    # stop. Not in a `finally`: a crash must leave the flag set, so the next start notices it.
    app.run_polling(allowed_updates=["message"])
    note_stop(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
