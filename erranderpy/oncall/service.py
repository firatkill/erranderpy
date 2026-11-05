"""When the on-call agent speaks: alerts, the daily report and /oncall.

- check(): every 5 minutes, looks at the last 15. Critical signals become an
  alert at once, written by code, so it goes out even when the LLM is the
  problem. The same kind of alert is not repeated within 6 hours.
- report(): the last 24 hours (08:00 every day, or /oncall): stats, then the
  LLM's diagnosis of every signal.

Messages from here go to the owner but never into the chat history: "thanks"
after an alert must not reach the router as context about a meeting.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from erranderpy.core.db import Database
from erranderpy.core.jobs import DISABLED
from erranderpy.core.llm import LLMError, LLMProvider
from erranderpy.core.runtime import TooManySteps, run_agent
from erranderpy.core.tools import ToolContext
from erranderpy.core.tracing import Tracer
from erranderpy.oncall.canary import router_canary
from erranderpy.oncall.agent import MAX_STEPS, Finding, build_oncall_agent, signals_text
from erranderpy.oncall.signals import CRITICAL, Signal, Stats, detect, stats

log = logging.getLogger(__name__)

ALERT_WINDOW = timedelta(minutes=15)
ALERT_COOLDOWN = timedelta(hours=6)
REPORT_HOURS = 24
# When the LLM itself is broken, asking it for a diagnosis only fails again.
LLM_KINDS = {"llm_auth", "llm_bad_request", "llm_unavailable"}

MESSAGES = {
    "tr": {
        "alert_header": "🚨 Oncall uyarısı",
        "report_header": "🩺 Oncall raporu · son {hours} saat",
        "stats": "📊 {messages} mesaj · {calls} LLM çağrısı · ${cost} · p95 {p95} sn · {errors} başarısız mesaj",
        "all_good": "✅ Sorun görmedim.",
        "nothing_to_do": "✅ Sinyallere baktım ({count}); dikkat gerektiren bir şey yok.",
        "no_diagnosis": "(Teşhis yapılamadı: {reason}.)",
        "cause": "Neden",
        "fix": "Öneri",
        "evidence": "İz",
        "llm_auth": "🔑 LLM servisi API anahtarını reddediyor ({count} mesaj cevapsız kaldı). .env'deki anahtarı kontrol et, sonra `make doctor` çalıştır.",
        "llm_bad_request": "🧾 LLM servisi isteklerimizi reddediyor ({count}). Model adı (LLM_MODEL) ya da parametreler değişmiş olabilir.",
        "llm_unavailable": "🌩️ LLM servisine ulaşılamıyor ({count} hata). Sağlayıcının durum sayfasına bak; sürerse başka bir sağlayıcıya geç.",
        "internal_error": "🐞 Kodda hata: {count} mesaj \"bir şeyler ters gitti\" ile cevaplandı.",
        "scheduler": "⏰ {count} hatırlatmanın vakti geçti ama gönderilmedi: zamanlayıcı çalışmıyor olabilir.",
    },
    "en": {
        "alert_header": "🚨 On-call alert",
        "report_header": "🩺 On-call report · last {hours} hours",
        "stats": "📊 {messages} messages · {calls} LLM calls · ${cost} · p95 {p95} s · {errors} failed messages",
        "all_good": "✅ I saw no problems.",
        "nothing_to_do": "✅ I looked at the signals ({count}); nothing needs attention.",
        "no_diagnosis": "(No diagnosis: {reason}.)",
        "cause": "Cause",
        "fix": "Fix",
        "evidence": "Traces",
        "llm_auth": "🔑 The LLM provider rejects the API key ({count} messages unanswered). Check the key in .env, then run `make doctor`.",
        "llm_bad_request": "🧾 The LLM provider rejects our requests ({count}). The model name (LLM_MODEL) or parameters may have changed.",
        "llm_unavailable": "🌩️ The LLM provider can't be reached ({count} errors). Check its status page; switch providers if it lasts.",
        "internal_error": "🐞 A bug in our code: {count} messages got \"something went wrong\".",
        "scheduler": "⏰ {count} reminders are past due but were not sent: the scheduler may not be running.",
    },
}

SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


@dataclass
class Diagnosis:
    findings: list[Finding] | None  # None: the agent couldn't finish
    error: str | None = None
    text: str = ""  # what the model said if it didn't submit a report


class Oncall:
    def __init__(
        self,
        llm: LLMProvider,
        db: Database,
        agents: Callable[[], dict[str, Any]],
        language: str,
        repo_root: Path | None = None,
        env: Mapping[str, str] | None = None,
    ):
        env = os.environ if env is None else env
        self.llm = llm
        self.db = db
        self.agents = agents  # a function: the bot's agents can change while it runs
        self.language = language
        self.repo_root = repo_root
        self.budget = float(env.get("ONCALL_DAILY_BUDGET_USD", "").strip() or 1.0)
        self.canary = env.get("ONCALL_CANARY", "").strip().lower() not in DISABLED
        self.agent = build_oncall_agent(language)

    # --- public --------------------------------------------------------------------

    def check(self, now: datetime | None = None) -> str | None:
        """An alert for new critical problems in the last minutes, or None."""
        now = now or datetime.now(UTC)
        critical = [s for s in detect(self.db, now - ALERT_WINDOW, now, self.budget) if s.severity == CRITICAL]
        fresh = [s for s in critical if not self._recently_alerted(s.kind, now)]
        if not fresh:
            return None
        for s in fresh:
            self.db.set_setting(f"oncall.alerted.{s.kind}", now.isoformat(timespec="seconds"))
        lines = [self._t("alert_header"), *(self._signal_line(s) for s in fresh)]
        if not any(s.kind in LLM_KINDS for s in fresh):
            hours = ALERT_WINDOW.total_seconds() / 3600
            lines += ["", self._diagnosis_text(self.diagnose(fresh, stats(self.db, now - ALERT_WINDOW), hours), fresh)]
        return "\n".join(lines).strip()

    def report(self, hours: float = REPORT_HOURS, now: datetime | None = None, canary: bool = True) -> str:
        """Stats and a diagnosis of everything that looked wrong in the last `hours`.

        `canary`: also run the router eval on the running agents (see canary.py).
        """
        now = now or datetime.now(UTC)
        since = now - timedelta(hours=hours)
        st = stats(self.db, since)
        signals = detect(self.db, since, now, self.budget)
        if canary and self.canary and not any(s.kind in LLM_KINDS for s in signals):
            try:
                if (regression := router_canary(self.llm, self.agents())) is not None:
                    signals.append(regression)
            except Exception:
                log.exception("on-call canary failed")  # a report without it is still a report
        lines = [self._t("report_header", hours=f"{hours:g}"), self._stats_line(st), ""]
        lines += [self._signal_line(s) for s in signals if s.severity == CRITICAL]
        if not signals:
            lines.append(self._t("all_good"))
        elif all(s.kind in LLM_KINDS for s in signals):
            pass  # the code lines above say it all; the LLM can't be asked
        else:
            lines.append(self._diagnosis_text(self.diagnose(signals, st, hours), signals))
        return "\n".join(lines).strip()

    def diagnose(self, signals: list[Signal], st: Stats, hours: float) -> Diagnosis:
        tracer = Tracer(self.db)
        ctx = ToolContext(
            db=self.db, user_id=0, chat_id=0, now=datetime.now(UTC), tracer=tracer,
            extras={"agents": self.agents(), "repo_root": self.repo_root},
        )
        text = signals_text(signals, st, hours)
        try:
            with tracer.span("oncall.run", agent="oncall", input=text) as span:
                said = run_agent(self.llm, self.agent, text, ctx, [], max_steps=MAX_STEPS)
                findings = ctx.effects.get("report")
                span.output = [f.model_dump() for f in findings] if findings is not None else said
        except LLMError as exc:
            log.warning("on-call diagnosis failed: %s", exc)
            return Diagnosis(None, f"LLM {exc.kind}")
        except TooManySteps:
            return Diagnosis(None, f"no report after {MAX_STEPS} steps")
        except Exception as exc:
            # A broken diagnosis must not swallow the alert: the code-written part still goes out.
            log.exception("on-call diagnosis crashed")
            return Diagnosis(None, type(exc).__name__)
        if findings is None:
            return Diagnosis(None, "no report", text=said)
        return Diagnosis(findings)

    # --- formatting ---------------------------------------------------------------

    def _diagnosis_text(self, d: Diagnosis, signals: list[Signal]) -> str:
        if d.findings is None:
            raw = "\n".join(f"• {s.kind} ×{s.count}: {s.detail}" for s in signals)
            return "\n".join(p for p in (self._t("no_diagnosis", reason=d.error), d.text, raw) if p)
        if not d.findings:
            return self._t("nothing_to_do", count=len(signals))
        blocks = []
        for f in d.findings:
            block = [
                f"{SEVERITY_ICON[f.severity]} {f.title}",
                f"   {self._t('cause')}: {f.root_cause}",
                f"   {self._t('fix')}: {f.suggestion}",
            ]
            if f.evidence:
                block.append(f"   {self._t('evidence')}: {', '.join(f.evidence[:3])}")
            blocks.append("\n".join(block))
        return "\n\n".join(blocks)

    def _signal_line(self, s: Signal) -> str:
        if s.kind in MESSAGES["en"]:
            return self._t(s.kind, count=s.count)
        return f"• {s.kind} ×{s.count}: {s.detail}"

    def _stats_line(self, st: Stats) -> str:
        return self._t(
            "stats", messages=st.messages, calls=st.llm_calls, cost=f"{st.cost_usd:.4f}",
            p95=f"{(st.p95_ms or 0) / 1000:.1f}", errors=st.errors,
        )

    def _recently_alerted(self, kind: str, now: datetime) -> bool:
        last = self.db.get_setting(f"oncall.alerted.{kind}")
        return last is not None and now - datetime.fromisoformat(last) < ALERT_COOLDOWN

    def _t(self, key: str, **values: Any) -> str:
        table = MESSAGES.get(self.language, MESSAGES["en"])
        return table.get(key, MESSAGES["en"][key]).format(**values)
