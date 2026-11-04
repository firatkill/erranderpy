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
from zoneinfo import ZoneInfo

from erranderpy.core import messages
from erranderpy.core.db import Database
from erranderpy.core.jobs import Job
from erranderpy.core.llm import LLMError, LLMProvider, Message
from erranderpy.core.registry import Agent
from erranderpy.core.router import UNKNOWN, Route, route
from erranderpy.core.runtime import TooManySteps, run_agent
from erranderpy.core.tools import ToolContext
from erranderpy.core.tracing import Tracer, load_trace

log = logging.getLogger(__name__)

# How many earlier messages (user + assistant) the model sees as context.
HISTORY_MESSAGES = 6

FALLBACK_PROMPT = """\
You are the assistant of a personal Telegram bot. You can only help with:
{abilities}
The user's message does not match these. Reply in one or two short sentences, in the user's language:
answer greetings and thanks naturally; for anything else, say you can't do that yet and mention what you can do.
Never claim to have done something."""


class Assistant:
    def __init__(self, llm: LLMProvider, agents: dict[str, Agent], db: Database, timezone: ZoneInfo, language: str):
        self.llm = llm
        self.agents = agents
        self.db = db
        self.timezone = timezone
        self.language = language
        # One message at a time: the bot has a single owner, and this keeps
        # tool writes and history in order.
        self._lock = threading.Lock()

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

    # --- steps ---------------------------------------------------------------

    def run_job(self, job: Job, user_id: int, chat_id: int) -> str | None:
        """Run one scheduled job for the owner. Returns text to send, or None."""
        agent = self.agents[job.agent]
        with self._lock:
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

    def _context(self, agent: Agent, user_id: int, chat_id: int, tracer: Tracer) -> ToolContext:
        return ToolContext(
            db=self.db,
            user_id=user_id,
            chat_id=chat_id,
            now=datetime.now(self.timezone),
            messages=agent.messages_for(self.language),
            tracer=tracer,
        )

    def _handle_route(self, r: Route, user_id: int, chat_id: int, history: list[Message], tracer: Tracer) -> str:
        agent = self.agents.get(r.agent)
        if agent is None or r.agent == UNKNOWN:
            return self._fallback(r.text, history, tracer)
        return run_agent(self.llm, agent, r.text, self._context(agent, user_id, chat_id, tracer), history)

    def _fallback(self, text: str, history: list[Message], tracer: Tracer) -> str:
        abilities = "\n".join(f"- {a.description}" for a in self.agents.values()) or "- nothing yet"
        with tracer.span("fallback.llm", agent=UNKNOWN, input=text) as span:
            result = self.llm.complete(FALLBACK_PROMPT.format(abilities=abilities), [*history, Message.user(text)], max_tokens=512)
            span.record_llm(result)
            span.output = result.text
        return result.text.strip()

    def _history(self, chat_id: int) -> list[Message]:
        rows = self.db.recent_messages(chat_id, HISTORY_MESSAGES)
        return [Message(role=row["role"], text=row["text"]) for row in rows]

    # --- commands --------------------------------------------------------------

    def help_text(self) -> str:
        agents = "\n".join(f"• {a.name}: {a.examples[0] if a.examples else a.description}" for a in self.agents.values())
        return self._t("help", agents=agents)

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
