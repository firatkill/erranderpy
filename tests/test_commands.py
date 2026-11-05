"""The "/" menu, /help and the handlers must list the same commands."""

import re
from zoneinfo import ZoneInfo

from telegram.ext import CommandHandler

from erranderpy.assistant import Assistant
from erranderpy.core import messages
from erranderpy.core.db import Database
from erranderpy.core.registry import load_agents
from erranderpy.gateway.telegram import build_application
from erranderpy.oncall.service import Oncall
from erranderpy.pairing import Pairing
from tests.fakes import ScriptedLLM


def test_every_menu_command_has_a_handler_and_is_in_help():
    db = Database(":memory:")
    assistant = Assistant(ScriptedLLM([]), load_agents(db=db), db, ZoneInfo("Europe/Istanbul"), "tr")
    oncall = Oncall(ScriptedLLM([]), db, lambda: assistant.agents, "tr")
    app = build_application("123456:TEST-TOKEN", assistant, Pairing(db), "tr", ZoneInfo("Europe/Istanbul"), oncall)
    handled = {c for group in app.handlers.values() for h in group if isinstance(h, CommandHandler) for c in h.commands}
    help_text = assistant.help_text()
    for language in messages.SUPPORTED_LANGUAGES:
        names = [name for name, _ in messages.commands(language)]
        assert set(names) <= handled
        for name, about in messages.commands(language):
            # Telegram's rules: 1-32 lowercase letters, digits, underscores; description 1-256 chars
            assert re.fullmatch(r"[a-z0-9_]{1,32}", name) and 1 <= len(about) <= 256
    assert all(f"/{name} · " in help_text for name, _ in messages.commands("tr"))
