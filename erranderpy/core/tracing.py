"""Record every step of handling a message, so we can see *why* the bot did what it did.

One incoming message = one trace (a short random `trace_id`). Each step inside
it (router decision, LLM call, tool call) is a span: a row in the `traces`
table with its input, output, duration, tokens, cost and error.
The on-call agent (Stage 3) reads this table.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from erranderpy.core.db import Database, utc_now_iso
from erranderpy.core.llm import LLMResult

MAX_FIELD_CHARS = 2000


@dataclass
class Span:
    step: str
    agent: str | None = None
    tool: str | None = None
    input: Any = None
    output: Any = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    latency_ms: float | None = None

    def record_llm(self, result: LLMResult) -> None:
        self.tokens_in = result.tokens_in
        self.tokens_out = result.tokens_out
        self.cost_usd = result.cost_usd


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + "…"


class Tracer:
    def __init__(self, db: Database | None, trace_id: str | None = None):
        self.db = db  # None = don't store (used by evals)
        self.trace_id = trace_id or uuid.uuid4().hex[:12]

    @contextmanager
    def span(self, step: str, *, agent: str | None = None, tool: str | None = None, input: Any = None) -> Iterator[Span]:
        span = Span(step=step, agent=agent, tool=tool, input=input)
        started = time.perf_counter()
        try:
            yield span
        except Exception as exc:
            span.error = span.error or f"{type(exc).__name__}: {exc}"
            raise
        finally:
            span.latency_ms = (time.perf_counter() - started) * 1000
            self._write(span)

    def _write(self, span: Span) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT INTO traces (trace_id, step, agent, tool, input, output, latency_ms, tokens_in, "
            "tokens_out, cost_usd, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.trace_id,
                span.step,
                span.agent,
                span.tool,
                _to_text(span.input),
                _to_text(span.output),
                round(span.latency_ms or 0, 1),
                span.tokens_in,
                span.tokens_out,
                span.cost_usd,
                span.error,
                utc_now_iso(),
            ),
        )


def load_trace(db: Database, trace_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in db.query("SELECT * FROM traces WHERE trace_id = ? ORDER BY id", (trace_id,))]
