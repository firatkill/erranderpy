"""The message pipeline: text in, reply out. No Telegram code here.

    history ─▶ router ─▶ for each route: agent loop (or fallback) ─▶ reply
                 └────────── every step traced under one trace_id ──────────┘

Keeping this free of Telegram means tests (and later other gateways) can drive
the whole bot with plain function calls.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from erranderpy.core import messages, reminders
from erranderpy.core.agent import Agent
from erranderpy.core.db import Database
from erranderpy.core.jobs import Job
from erranderpy.core.llm import LLMError, LLMProvider, Message
from erranderpy.core.registry import AGENTS_DIR, load_agents
from erranderpy.core.router import UNKNOWN, Route, route
from erranderpy.core.runtime import TooManySteps, run_agent
from erranderpy.core.tools import ToolContext
from erranderpy.core.tracing import Tracer, load_trace

log = logging.getLogger(__name__)

# How many earlier messages (user + assistant) the model sees as context.
HISTORY_MESSAGES = 6

FALLBACK_PROMPT = """\
You are the assistant of a personal Telegram bot. This is everything you can do:
{abilities}
The user's message does not match these. Reply in one or two short sentences, in the user's language:
answer greetings and thanks naturally; for questions about what you can do, answer from the list above
(including anything you do automatically at set times); otherwise say you can't do that yet.
Never claim to have done something, and never claim you can't do something that is in the list."""


class Assistant:
    def __init__(
        self,
        llm: LLMProvider,
        agents: dict[str, Agent],
        db: Database,
        timezone: ZoneInfo,
        language: str,
        agent_dirs: list[Path] | None = None,
    ):
        self.llm = llm
        self.agents = agents
        self.db = db
        self.timezone = timezone
        self.language = language
        self.agent_dirs = agent_dirs or [AGENTS_DIR]
        # One message at a time: the bot has a single owner, and this keeps
        # tool writes and history in order.
        self._lock = threading.RLock()

    @property
    def user_agents_dir(self) -> Path | None:
        """Where the bot writes agents it creates: the last of several agent folders.

        With a single folder (the built-in agents/) the bot must not write
        anywhere, so creating abilities is off.
        """
        return self.agent_dirs[-1] if len(self.agent_dirs) > 1 else None

    def reload_agents(self) -> None:
        """Pick up agents added or removed while running (the builder does this)."""
        self.agents = load_agents(self.agent_dirs, self.db)
        log.info("agents reloaded: %s", ", ".join(self.agents))

    # --- messages --------------------------------------------------------------

    def handle(self, user_id: int, chat_id: int, text: str) -> str:
        with self._lock:
            tracer = Tracer(self.db)
            history = self._history(chat_id)
            try:
                with tracer.span("message", input=text) as total:
                    routes = route(self.llm, list(self.agents.values()), text, history, tracer)
                    replies = [self._handle_route(r, user_id, chat_id, history, tracer) for r in routes]
                    reply = "\n\n".join(r for r in replies if r) or self._t("empty_reply")
                    total.output = reply
            except LLMError as exc:
                log.warning("LLM error (%s) in trace %s: %s", exc.kind, tracer.trace_id, exc)
                reply = self._t("error_auth") if exc.kind == "auth" else self._t("error_llm", kind=exc.kind)
            except TooManySteps:
                reply = self._t("too_many_steps")
            except Exception:
                log.exception("unexpected error in trace %s", tracer.trace_id)
                reply = self._t("error_internal")

            self.db.save_message(chat_id, "user", text, tracer.trace_id)
            self.db.save_message(chat_id, "assistant", reply, tracer.trace_id)
            return reply

    def _handle_route(
        self, r: Route, user_id: int, chat_id: int, history: list[Message], tracer: Tracer, replayed: bool = False
    ) -> str:
        agent = self.agents.get(r.agent)
        if agent is None or r.agent == UNKNOWN:
            return self._fallback(r.text, history, tracer)
        ctx = self._context(agent, user_id, chat_id, tracer, r.text)
        reply = run_agent(self.llm, agent, r.text, ctx, history, r.requests)

        # Effects a tool asked for (the builder after creating an agent):
        if ctx.effects.get("reload_agents"):
            self.reload_agents()
        replay = ctx.effects.get("replay")
        if replay and not replayed:
            # Handle the request that started it all ("11 kasım 14:00 AVM") with the
            # agent that now exists. Only once: a replay never triggers another replay.
            routes = route(self.llm, list(self.agents.values()), replay, [], tracer)
            extra = [self._handle_route(x, user_id, chat_id, [], tracer, replayed=True) for x in routes]
            reply = "\n\n".join([reply, *(e for e in extra if e)])
        return reply

    def _fallback(self, text: str, history: list[Message], tracer: Tracer) -> str:
        with tracer.span("fallback.llm", agent=UNKNOWN, input=text) as span:
            prompt = FALLBACK_PROMPT.format(abilities=self.capabilities())
            result = self.llm.complete(prompt, [*history, Message.user(text)], max_tokens=512)
            span.record_llm(result)
            span.output = result.text
        return result.text.strip()

    def capabilities(self) -> str:
        """What the bot can do, built from the agents themselves, so it is never out of date.

        Includes scheduled jobs and reminders: without them the model once told
        the user it "can't send reminders" while an evening summary was running.
        """
        lines = []
        for a in self.agents.values():
            line = f"- {a.title or a.name}: {a.description}"
            for job in a.jobs:
                at = job.time_of_day()
                doc = (job.fn.__doc__ or job.name).strip().splitlines()[0]
                if at is not None:
                    line += f" Automatically every day at {at:%H:%M}: {doc}"
            if a.reminder_hint:
                line += f" It sends reminders ({a.reminder_hint})."
            lines.append(line)
        return "\n".join(lines) or "- nothing yet"

    def _context(self, agent: Agent, user_id: int, chat_id: int, tracer: Tracer, request_text: str = "") -> ToolContext:
        return ToolContext(
            db=self.db,
            user_id=user_id,
            chat_id=chat_id,
            now=datetime.now(self.timezone),
            messages=agent.messages_for(self.language),
            tracer=tracer,
            request_text=request_text,
            extras={"agents": self.agents, "user_agents_dir": self.user_agents_dir},
        )

    def _history(self, chat_id: int) -> list[Message]:
        rows = self.db.recent_messages(chat_id, HISTORY_MESSAGES)
        return [Message(role=row["role"], text=row["text"]) for row in rows]

    # --- scheduled work -----------------------------------------------------------

    def run_job(self, job: Job, user_id: int, chat_id: int) -> str | None:
        """Run one scheduled job for the owner. Returns text to send, or None."""
        with self._lock:
            agent = self.agents.get(job.agent)
            if agent is None:
                return None
            tracer = Tracer(self.db)
            ctx = self._context(agent, user_id, chat_id, tracer)
            try:
                with tracer.span("job.run", agent=agent.name, tool=job.name) as span:
                    text = job.fn(ctx)
                    span.output = text
            except Exception:
                log.exception("job %s.%s failed (trace %s)", agent.name, job.name, tracer.trace_id)
                return None
            if text:
                # Keep it in the history so "thanks!" after the summary has context.
                self.db.save_message(chat_id, "assistant", text, tracer.trace_id)
            return text or None

    def due_reminders(self, chat_id: int) -> list[str]:
        """Claim reminders that are due now; returns the texts to send."""
        with self._lock:
            texts = []
            for due in reminders.take_due(self.db):
                text = (self._t("late_prefix") + " " if due.late else "") + due.text
                tracer = Tracer(self.db)
                with tracer.span("reminder.send", agent=due.agent, input=due.id) as span:
                    span.output = text
                self.db.save_message(chat_id, "assistant", text, tracer.trace_id)
                texts.append(text)
            return texts

    # --- commands --------------------------------------------------------------

    def help_text(self) -> str:
        agents = "\n".join(
            f"• {a.title or a.name}: {a.examples[0] if a.examples else a.description}" for a in self.agents.values()
        )
        return self._t("help", agents=agents)

    def agents_text(self) -> str:
        lines = [self._t("agents_header")]
        for a in self.agents.values():
            origin = self._t("agent_by_bot") if a.created_by == "builder" else self._t("agent_builtin")
            lines.append(f"• {a.title or a.name} ({a.name}) · {origin}")
        return "\n".join(lines)

    def status_text(self) -> str:
        midnight = datetime.combine(datetime.now(self.timezone).date(), time.min, tzinfo=self.timezone)
        since = midnight.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        row = self.db.one(
            "SELECT COUNT(DISTINCT trace_id) AS messages, "
            "SUM(CASE WHEN tokens_in IS NOT NULL THEN 1 ELSE 0 END) AS calls, "
            "COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0) AS tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost FROM traces WHERE created_at >= ?",
            (since,),
        )
        return self._t(
            "status",
            provider=self.llm.name,
            model=self.llm.model,
            messages=row["messages"] or 0,
            calls=row["calls"] or 0,
            tokens=row["tokens"] or 0,
            cost=f"{row['cost'] or 0:.4f}",
        )

    def last_trace_text(self, chat_id: int) -> str:
        row = self.db.one(
            "SELECT trace_id FROM messages WHERE chat_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1", (chat_id,)
        )
        if row is None:
            return self._t("no_trace")
        return format_trace(row["trace_id"], load_trace(self.db, row["trace_id"]))

    def _t(self, key: str, **values) -> str:
        return messages.t(self.language, key, **values)


def format_trace(trace_id: str, spans: list[dict]) -> str:
    """A compact, human-readable view of one trace (used by /trace)."""
    lines = [f"🔎 trace {trace_id}"]
    total_cost = 0.0
    for s in spans:
        label = s["step"] + (f" [{s['agent']}]" if s["agent"] else "") + (f" {s['tool']}" if s["tool"] else "")
        seconds = (s["latency_ms"] or 0) / 1000
        line = f"• {label} · {seconds:.2f}s"
        if s["tokens_in"] is not None:
            line += f" · {s['tokens_in']}+{s['tokens_out']} tok"
        if s["error"]:
            line += f" · ⚠️ {s['error']}"
        if s["step"] in ("router.decide", "tool.call") and s["output"]:
            line += f"\n   → {s['output'][:200]}"
        lines.append(line)
        total_cost += s["cost_usd"] or 0
    lines.append(f"💰 ${total_cost:.6f}")
    return "\n".join(lines)
