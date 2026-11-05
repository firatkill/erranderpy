"""Telegram gateway: the only file that knows about Telegram.

It receives messages by long polling (no open port, no domain; ADR-0001),
checks who is writing (pairing), shows "typing…" while the pipeline works,
and sends the reply back. All the thinking happens in `Assistant`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from erranderpy.assistant import Assistant
from erranderpy.core import messages
from erranderpy.core.jobs import DISABLED, Job, parse_hhmm
from erranderpy.oncall.service import Oncall
from erranderpy.pairing import OWNER_CHAT_KEY, Pairing

log = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4096
TYPING_REFRESH_S = 4  # Telegram shows "typing…" for ~5 s per call
REMINDER_CHECK_S = 30  # how often due reminders are looked up
ONCALL_CHECK_S = 300  # how often the on-call agent looks for critical problems


def build_application(
    token: str,
    assistant: Assistant,
    pairing: Pairing,
    language: str,
    timezone: ZoneInfo,
    oncall: Oncall | None = None,
    oncall_report_at: str = "08:00",
    startup_notice: str | None = None,
) -> Application:
    async def register_commands(app: Application) -> None:
        """Fill the menu Telegram shows when you type "/": command + description.

        The default list is in BOT_LANGUAGE; people whose Telegram app is set to
        another supported language see theirs. A failure here must not stop the bot.
        """
        try:
            await app.bot.set_my_commands(messages.commands(language))
            for code in messages.SUPPORTED_LANGUAGES:
                await app.bot.set_my_commands(messages.commands(code), language_code=code)
        except Exception:
            log.warning("could not register the command menu with Telegram", exc_info=True)

    async def on_start(app: Application) -> None:
        await register_commands(app)
        chat_id = oncall_chat()
        if startup_notice and chat_id is not None:
            try:
                await app.bot.send_message(chat_id, startup_notice)
            except Exception:
                log.warning("could not send the restart notice", exc_info=True)

    def oncall_chat() -> int | None:
        """Where on-call messages go: ONCALL_CHAT_ID (e.g. a separate group), else the owner."""
        chat = os.environ.get("ONCALL_CHAT_ID", "").strip() or assistant.db.get_setting(OWNER_CHAT_KEY)
        return int(chat) if chat else None

    app = Application.builder().token(token).post_init(on_start).build()

    async def guard(update: Update) -> bool:
        """True if the sender is the owner. Handles pairing on the way."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if user is None or chat is None or msg is None:
            return False
        if pairing.is_owner(user.id):
            return True
        if not pairing.paired and not pairing.blocked(user.id):
            if pairing.try_pair(user.id, chat.id, msg.text or ""):
                log.info("paired with Telegram user %s", user.id)
                await msg.reply_text(messages.t(language, "paired"))
            elif not pairing.blocked(user.id):
                await msg.reply_text(messages.t(language, "not_paired"))
        # Not the owner: stay silent, don't reveal anything.
        return False

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        async with typing(context, chat.id):
            # The pipeline is synchronous (SDK calls, SQLite); run it in a worker
            # thread so the bot keeps answering Telegram while we wait for the LLM.
            reply = await asyncio.to_thread(assistant.handle, user.id, chat.id, msg.text)
        await send(msg, reply)

    async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await send(update.effective_message, assistant.help_text())

    async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await send(update.effective_message, await asyncio.to_thread(assistant.status_text))

    async def on_trace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            text = await asyncio.to_thread(assistant.last_trace_text, update.effective_chat.id)
            await send(update.effective_message, text)

    async def on_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await send(update.effective_message, assistant.agents_text())

    async def send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = assistant.db.get_setting(OWNER_CHAT_KEY)
        if chat_id is None:
            return
        for text in await asyncio.to_thread(assistant.due_reminders, int(chat_id)):
            await context.bot.send_message(int(chat_id), text[:TELEGRAM_MAX_CHARS])

    async def run_job(context: ContextTypes.DEFAULT_TYPE) -> None:
        job: Job = context.job.data
        owner = pairing.owner_id
        chat_id = assistant.db.get_setting(OWNER_CHAT_KEY)
        if owner is None or chat_id is None:
            return  # not paired yet: nobody to run it for
        text = await asyncio.to_thread(assistant.run_job, job, owner, int(chat_id))
        if text:
            await context.bot.send_message(int(chat_id), text[:TELEGRAM_MAX_CHARS])

    # Daily jobs of agents loaded at start. (Agents the bot creates while running
    # are records agents: they use reminders, not daily jobs.)
    for agent in assistant.agents.values():
        for job in agent.jobs:
            at = job.time_of_day()
            if at is None:
                log.info("job %s.%s is turned off", agent.name, job.name)
                continue
            app.job_queue.run_daily(run_job, time=at.replace(tzinfo=timezone), data=job, name=f"{agent.name}.{job.name}")

    async def oncall_check(context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = oncall_chat()
        if oncall is None or chat_id is None:
            return
        try:
            text = await asyncio.to_thread(oncall.check)
        except Exception:
            log.exception("on-call check failed")  # the watcher must never crash the bot
            return
        if text:
            await context.bot.send_message(chat_id, text[:TELEGRAM_MAX_CHARS])

    async def oncall_report(context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = oncall_chat()
        if oncall is None or chat_id is None:
            return
        try:
            text = await asyncio.to_thread(oncall.report)
        except Exception:
            log.exception("on-call report failed")
            return
        await context.bot.send_message(chat_id, text[:TELEGRAM_MAX_CHARS])

    async def on_oncall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update) or oncall is None:
            return
        async with typing(context, update.effective_chat.id):
            text = await asyncio.to_thread(oncall.report)
        await send(update.effective_message, text)

    if oncall is not None:
        app.job_queue.run_repeating(oncall_check, interval=ONCALL_CHECK_S, first=60, name="oncall.check")
        if oncall_report_at.strip().lower() not in DISABLED:
            at = parse_hhmm(oncall_report_at).replace(tzinfo=timezone)
            app.job_queue.run_daily(oncall_report, time=at, name="oncall.report")
        app.add_handler(CommandHandler("oncall", on_oncall))

    # One-off reminders live in the database; check for due ones every 30 s.
    app.job_queue.run_repeating(send_reminders, interval=REMINDER_CHECK_S, first=5, name="core.reminders")

    app.add_handler(CommandHandler(["start", "help"], on_help))
    app.add_handler(CommandHandler("agents", on_agents))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("trace", on_trace))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


@asynccontextmanager
async def typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Keep "typing…" visible until the reply is ready."""

    async def keep_typing() -> None:
        while True:
            try:
                await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:  # cosmetic only; never fail the reply because of it
                log.debug("send_chat_action failed", exc_info=True)
            await asyncio.sleep(TYPING_REFRESH_S)

    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()


async def send(msg, text: str) -> None:
    # A long note (show_note) can pass Telegram's limit: send it in parts, not cut.
    for part in split_message(text):
        await msg.reply_text(part)


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Parts of at most `limit` chars, cut at a line break when there is one."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    return [*parts, text] if text else parts
