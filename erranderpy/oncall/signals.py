"""Detectors: plain code that reads the traces and says what looks wrong.

No LLM here. Detection must work when the LLM doesn't (a revoked API key is
exactly when you need the alert), and it must be cheap enough to run every few
minutes. The LLM only comes in afterwards, to explain *why* (oncall/agent.py).

Two families (SPEC §10):
- technical: LLM errors, exceptions, a stopped scheduler, slowness, cost
- semantic: the bot "worked" but did the wrong thing: messages routed to
  unknown, dropped requests (agent.nudge), the user asking "did you do it?"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC

from erranderpy.core.db import Database
from erranderpy.core.text import normalize

CRITICAL, WARNING, INFO = "critical", "warning", "info"

# Spans the on-call agent writes itself are not part of what it watches.
NOT_ONCALL = "step NOT LIKE 'oncall%' AND COALESCE(agent, '') != 'oncall'"

LLM_KIND = re.compile(r"\[[\w-]+:(\w+)\]")  # LLMError text: "[openai:auth] ..."
UNAVAILABLE = {"timeout", "connection", "server", "rate_limit"}

# The user doubting or correcting the bot: a sign something went wrong silently.
# Live, 2026-11-05: "8 kasıma toplantı koydun mu?" after a request had been dropped.
DOUBT = re.compile(
    r"\b(koydun|ekledin|yaptın|kaydettin|sildin|kurdun|ayarladın|hatırlattın|aldın|yazdın)\s*m[ıiuü]\b"
    r"|\böyle demedim\b|\byanlış (anladın|yaptın|ekledin|kaydettin)|\bgeri al\b|\bolmadı\b"
    r"|\bdid you\b|\bthat'?s wrong\b|\bi didn'?t say\b|\bundo\b",
    re.IGNORECASE,
)

REPEAT_WINDOW = timedelta(minutes=3)
OVERDUE_GRACE = timedelta(minutes=5)  # the reminder check runs every 30 s
SLOW_P95_MS = 15_000
MAX_EXAMPLES = 5


@dataclass
class Signal:
    kind: str
    severity: str
    count: int
    detail: str  # one line for the LLM and the report, English
    examples: list[str] = field(default_factory=list)  # short lines with trace ids


@dataclass
class Stats:
    messages: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    p50_ms: float | None = None
    p95_ms: float | None = None
    errors: int = 0


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def stats(db: Database, since: datetime) -> Stats:
    s = iso(since)
    row = db.one(
        f"SELECT COUNT(DISTINCT trace_id) AS m, SUM(tokens_in IS NOT NULL) AS c, COALESCE(SUM(cost_usd), 0) AS cost "
        f"FROM traces WHERE created_at >= ? AND {NOT_ONCALL}",
        (s,),
    )
    latencies = sorted(
        r["latency_ms"] for r in db.query("SELECT latency_ms FROM traces WHERE step = 'message' AND created_at >= ?", (s,))
    )
    errors = db.one("SELECT COUNT(*) AS n FROM traces WHERE step = 'message' AND error IS NOT NULL AND created_at >= ?", (s,))
    return Stats(
        messages=len(latencies),
        llm_calls=row["c"] or 0,
        cost_usd=row["cost"] or 0.0,
        p50_ms=_percentile(latencies, 0.50),
        p95_ms=_percentile(latencies, 0.95),
        errors=errors["n"],
    )


def detect(db: Database, since: datetime, now: datetime, daily_budget_usd: float = 1.0) -> list[Signal]:
    """Everything that looks wrong between `since` and `now`, most severe first."""
    s = iso(since)
    found: list[Signal] = []
    found += _llm_errors(db, s)
    found += _internal_errors(db, s)
    found += _simple(db, s, "TooManySteps%", "too_many_steps", WARNING, "the agent loop hit MAX_STEPS")
    found += _job_errors(db, s)
    found += _reminders(db, s, now)
    found += _unknown_routes(db, s)
    found += _spans(db, s, "agent.nudge", None, "dropped_request_check", INFO,
                    "the multi-request check ran (fewer tool calls than requests, or a check of several requests)")
    found += _spans(db, s, "tool.call", "error IN ('unknown_tool', 'invalid_arguments')", "tool_call_errors", INFO,
                    "the model called a tool that doesn't exist or with invalid arguments (it got a retry)")
    found += _user_doubt(db, s)
    found += _repeats(db, s)
    st = stats(db, since)
    if st.messages >= 5 and st.p95_ms and st.p95_ms > SLOW_P95_MS:
        found.append(Signal("slow", WARNING, st.messages, f"p95 reply time {st.p95_ms / 1000:.1f} s over {st.messages} messages"))
    hours = max((now - since).total_seconds() / 3600, 1)
    budget = daily_budget_usd * hours / 24
    if st.cost_usd > budget:
        found.append(Signal("cost", WARNING, st.llm_calls, f"spent ${st.cost_usd:.4f}, budget for this window ${budget:.4f}"))
    order = {CRITICAL: 0, WARNING: 1, INFO: 2}
    return sorted(found, key=lambda x: (order[x.severity], -x.count))


# --- detectors -------------------------------------------------------------------


def _llm_errors(db: Database, since: str) -> list[Signal]:
    rows = db.query(
        "SELECT trace_id, error FROM traces WHERE step = 'message' AND error LIKE 'LLMError%' AND created_at >= ? ORDER BY id",
        (since,),
    )
    by_kind: dict[str, list] = {}
    for r in rows:
        match = LLM_KIND.search(r["error"])
        by_kind.setdefault(match.group(1) if match else "unknown", []).append(r)
    signals = []
    for kind, items in by_kind.items():
        examples = [f"{r['trace_id']}: {redact(r['error'])[:160]}" for r in items[-MAX_EXAMPLES:]]
        if kind == "auth":
            signals.append(Signal("llm_auth", CRITICAL, len(items), "the LLM provider rejects the API key", examples))
        elif kind == "bad_request":
            signals.append(Signal("llm_bad_request", CRITICAL, len(items), "the LLM provider rejects our requests (model name? parameters?)", examples))
        else:
            # One timeout is weather; several in a row is an outage.
            severity = CRITICAL if len(items) >= 3 else WARNING
            signals.append(Signal("llm_unavailable", severity, len(items), f"LLM calls failed with '{kind}'", examples))
    return signals


def _internal_errors(db: Database, since: str) -> list[Signal]:
    rows = db.query(
        "SELECT trace_id, error FROM traces WHERE step = 'message' AND error IS NOT NULL "
        "AND error NOT LIKE 'LLMError%' AND error NOT LIKE 'TooManySteps%' AND created_at >= ? ORDER BY id",
        (since,),
    )
    if not rows:
        return []
    examples = []
    for r in rows[-MAX_EXAMPLES:]:
        # The first failing span inside the trace says where it broke.
        first = db.one(
            "SELECT step, agent, tool, error FROM traces WHERE trace_id = ? AND error IS NOT NULL ORDER BY id LIMIT 1",
            (r["trace_id"],),
        )
        where = ".".join(p for p in (first["step"], first["agent"], first["tool"]) if p)
        examples.append(f"{r['trace_id']}: {where}: {redact(first['error'])[:160]}")
    return [Signal("internal_error", CRITICAL, len(rows), "an exception in our code; the user got 'something went wrong'", examples)]


def _simple(db: Database, since: str, error_like: str, kind: str, severity: str, detail: str) -> list[Signal]:
    rows = db.query(
        "SELECT trace_id, input FROM traces WHERE step = 'message' AND error LIKE ? AND created_at >= ?", (error_like, since)
    )
    if not rows:
        return []
    return [Signal(kind, severity, len(rows), detail, [f"{r['trace_id']}: \"{r['input']}\"" for r in rows[-MAX_EXAMPLES:]])]


def _job_errors(db: Database, since: str) -> list[Signal]:
    rows = db.query(
        "SELECT trace_id, agent, tool, error FROM traces WHERE step = 'job.run' AND error IS NOT NULL AND created_at >= ?",
        (since,),
    )
    if not rows:
        return []
    examples = [f"{r['trace_id']}: {r['agent']}.{r['tool']}: {redact(r['error'])[:160]}" for r in rows[-MAX_EXAMPLES:]]
    return [Signal("job_failed", WARNING, len(rows), "a scheduled job raised an exception", examples)]


def _reminders(db: Database, since: str, now: datetime) -> list[Signal]:
    signals = []
    overdue = db.query(
        "SELECT id, agent, due_at, text FROM reminders WHERE status = 'pending' AND due_at < ? ORDER BY due_at",
        (iso(now - OVERDUE_GRACE),),
    )
    if overdue:
        examples = [f"reminder #{r['id']} ({r['agent']}) due {r['due_at']}: {r['text'][:80]}" for r in overdue[:MAX_EXAMPLES]]
        signals.append(
            Signal("scheduler", CRITICAL, len(overdue), "reminders are past due but still pending: the reminder check is not running", examples)
        )
    late = db.query(
        "SELECT id, status, due_at, sent_at FROM reminders WHERE status IN ('late', 'missed') AND COALESCE(sent_at, due_at) >= ?",
        (since,),
    )
    if late:
        examples = [f"reminder #{r['id']} {r['status']}: due {r['due_at']}, sent {r['sent_at']}" for r in late[:MAX_EXAMPLES]]
        signals.append(Signal("reminders_late", WARNING, len(late), "reminders went out late or not at all (was the bot stopped?)", examples))
    return signals


def _unknown_routes(db: Database, since: str) -> list[Signal]:
    rows = db.query(
        f"SELECT trace_id, output FROM traces WHERE step = 'router.decide' AND created_at >= ? AND {NOT_ONCALL} "
        "AND (output LIKE 'unknown:%' OR output LIKE '%| unknown:%' OR error = 'no_tool_call') ORDER BY id",
        (since,),
    )
    if not rows:
        return []
    examples = []
    for r in rows[-MAX_EXAMPLES:]:
        parts = [p.strip() for p in (r["output"] or "").split(" | ") if p.strip().startswith("unknown:")]
        texts = "; ".join(p.removeprefix("unknown:").strip() for p in parts) or r["output"]
        examples.append(f"{r['trace_id']}: \"{texts[:120]}\"")
    # Greetings and thanks go to unknown too; only several in a window are worth a look.
    severity = WARNING if len(rows) >= 3 else INFO
    return [Signal("unknown_routes", severity, len(rows), "messages the router sent to 'unknown' (no agent fit)", examples)]


def _spans(db: Database, since: str, step: str, where: str | None, kind: str, severity: str, detail: str) -> list[Signal]:
    extra = f" AND {where}" if where else ""
    rows = db.query(
        f"SELECT trace_id, agent, tool, output, error FROM traces WHERE step = ? AND created_at >= ? AND {NOT_ONCALL}{extra} ORDER BY id",
        (step, since),
    )
    if not rows:
        return []
    examples = []
    for r in rows[-MAX_EXAMPLES:]:
        what = r["error"] or r["output"] or ""
        examples.append(f"{r['trace_id']}: {r['agent'] or ''} {r['tool'] or ''} {redact(what)[:120]}".replace("  ", " "))
    return [Signal(kind, severity, len(rows), detail, examples)]


def _user_doubt(db: Database, since: str) -> list[Signal]:
    rows = db.query("SELECT id, chat_id, text, trace_id FROM messages WHERE role = 'user' AND created_at >= ? ORDER BY id", (since,))
    hits = []
    for r in rows:
        if not DOUBT.search(r["text"]):
            continue
        before = db.one(
            "SELECT text FROM messages WHERE chat_id = ? AND id < ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
            (r["chat_id"], r["id"]),
        )
        previous = (before["text"] if before else "").replace("\n", " ")[:100]
        hits.append(f"{r['trace_id']}: user \"{r['text'][:100]}\" after bot \"{previous}\"")
    if not hits:
        return []
    return [Signal("user_doubt", WARNING, len(hits), "the user questioned or corrected the bot: maybe it did the wrong thing", hits[-MAX_EXAMPLES:])]


def _repeats(db: Database, since: str) -> list[Signal]:
    rows = db.query("SELECT text, trace_id, created_at FROM messages WHERE role = 'user' AND created_at >= ? ORDER BY id", (since,))
    hits, last_seen = [], {}
    for r in rows:
        key = normalize(r["text"])
        at = datetime.fromisoformat(r["created_at"])
        if key in last_seen and at - last_seen[key] <= REPEAT_WINDOW:
            hits.append(f"{r['trace_id']}: \"{r['text'][:100]}\"")
        last_seen[key] = at
    if not hits:
        return []
    return [Signal("repeated_message", INFO, len(hits), "the user sent the same message again within 3 minutes", hits[-MAX_EXAMPLES:])]


# --- helpers ------------------------------------------------------------------------

_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-*]{6,}|AIza[0-9A-Za-z_\-]{10,}|\b\d{8,10}:[A-Za-z0-9_\-]{30,})")


def redact(text: str) -> str:
    """Hide anything that looks like an API key or bot token before it is shown or sent.

    Provider errors quote part of the key ("Incorrect API key provided: sk-proj-…").
    """
    return _SECRET.sub("[redacted]", text or "")


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1)))
    return sorted_values[index]
