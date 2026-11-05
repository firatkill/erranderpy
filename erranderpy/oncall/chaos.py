"""Chaos test: break the bot on purpose and see whether on-call notices and explains it.

    uv run python -m erranderpy.oncall.chaos     (make chaos)

Each scenario runs the real pipeline (real agents, the real LLM) against a
throwaway in-memory database with one fault injected, then asks two questions:

- alert:     did the code-written check() raise an alert? (critical faults only)
- diagnosis: did the on-call LLM name the right category (and the right place)?

The diagnosis always uses a healthy LLM, even when the fault is a broken key:
it measures the agent's reasoning. (In real life a broken key also breaks the
diagnosis; that's why the alert itself is written by code.)
Costs a few cents. SPEC §12 target: at least 4 of 5 diagnosed correctly.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from erranderpy.assistant import Assistant
from erranderpy.config import ROOT
from erranderpy.core import reminders
from erranderpy.core.db import Database
from erranderpy.core.llm import LLMError, LLMProvider, select_provider
from erranderpy.core.registry import load_agents
from erranderpy.oncall.canary import router_canary
from erranderpy.oncall.service import Oncall
from erranderpy.oncall.signals import detect, stats

TZ = ZoneInfo("Europe/Istanbul")
USER = CHAT = 1


class FaultyLLM:
    """The real provider, unless a fault is switched on."""

    def __init__(self, real: LLMProvider):
        self.real = real
        self.name, self.model = real.name, real.model
        self.fault: LLMError | None = None

    def complete(self, *args, **kwargs):
        if self.fault is not None:
            raise self.fault
        return self.real.complete(*args, **kwargs)


@dataclass
class Env:
    db: Database
    assistant: Assistant
    llm: FaultyLLM

    def say(self, *texts: str) -> None:
        for text in texts:
            self.assistant.handle(USER, CHAT, text)


@dataclass
class Scenario:
    name: str
    fault: str  # what we break, in words
    expect: str  # the category a correct diagnosis names
    inject: Callable[[Env], None]
    mention: tuple[str, ...] = ()  # a correct diagnosis also says each of these (regexes): the place and the cause
    alert: bool = True  # a critical fault: check() must alert


def bad_key(env: Env) -> None:
    env.llm.fault = LLMError("auth", "Error code: 401 - Incorrect API key provided: sk-proj-****wxyz", env.llm.name)
    env.say("süt al", "bugün 20 dk ingilizce çalıştım", "selam")


def timeouts(env: Env) -> None:
    env.say("süt al")  # works
    env.llm.fault = LLMError("timeout", "Request timed out.", env.llm.name)
    env.say("ekmek al", "listemde ne var", "yumurta da ekle", "bugünkü görevlerim ne")


def scheduler_stopped(env: Env) -> None:
    # The reminder job never ran: reminders stay pending after their time.
    now = datetime.now(TZ)
    reminders.schedule(env.db, USER, "calendar", now - timedelta(minutes=40), "⏰ Hatırlatma: Dişçi · 10:00", 1)
    reminders.schedule(env.db, USER, "calendar", now - timedelta(minutes=12), "⏰ Hatırlatma: Toplantı · 11:00", 2)
    env.say("süt al")


def broken_description(env: Env) -> None:
    # A bad edit to agents/shopping/agent.yaml: the router no longer knows what shopping does.
    shopping = env.assistant.agents["shopping"]
    env.assistant.agents["shopping"] = replace(
        shopping, description="Answers questions about the weather forecast.", examples=["yarın yağmur yağacak mı"]
    )
    env.say("zeytinyağı almam lazım", "süt ve ekmek ekle", "listemde ne var", "deterjan bitti, listeye yaz")


def tool_bug(env: Env) -> None:
    # A bug in agents/shopping/tools.py: add_items crashes on every call.
    tools = env.assistant.agents["shopping"].tools
    add = tools["add_items"]

    def crashing(ctx, **kwargs):
        raise KeyError("quantity")

    tools["add_items"] = replace(add, fn=crashing)
    env.say("zeytinyağı al", "listemde ne var", "2 paket makarna ekle")


SCENARIOS = [
    Scenario("bad_key", "LLM API anahtarı bozuk", "llm_auth", bad_key),
    Scenario("timeouts", "LLM servisi zaman aşımına uğruyor", "llm_unavailable", timeouts),
    Scenario("scheduler_stopped", "hatırlatma zamanlayıcısı çalışmıyor", "scheduler", scheduler_stopped),
    Scenario("broken_description", "shopping'in açıklaması bozuldu", "routing", broken_description,
             mention=(r"shopping|alışveriş", r"description|açıklama"), alert=False),
    Scenario("tool_bug", "add_items her çağrıda çöküyor", "tool_bug", tool_bug, mention=(r"add_items", r"KeyError|quantity")),
]


@dataclass
class Outcome:
    scenario: Scenario
    signals: list[str]
    alerted: bool
    categories: list[str]
    diagnosed: bool
    summary: str


def run_scenario(real: LLMProvider, scenario: Scenario) -> Outcome:
    db = Database(":memory:")
    llm = FaultyLLM(real)
    assistant = Assistant(llm, load_agents(db=db), db, TZ, "tr")
    env = Env(db, assistant, llm)
    scenario.inject(env)

    oncall = Oncall(real, db, lambda: assistant.agents, "tr", repo_root=ROOT)
    alert = oncall.check()
    now = datetime.now(UTC)
    since = now - timedelta(hours=1)
    signals = detect(db, since, now)
    if not any(s.kind.startswith("llm_") for s in signals) and (regression := router_canary(real, assistant.agents)):
        signals.append(regression)  # as the daily report does
    diagnosis = oncall.diagnose(signals, stats(db, since), 1)
    findings = diagnosis.findings or []
    categories = [f.category for f in findings]
    hit = next((f for f in findings if f.category == scenario.expect), None)
    text = " ".join(f"{f.title} {f.root_cause} {f.suggestion}" for f in findings)
    diagnosed = hit is not None and all(re.search(m, text, re.IGNORECASE) for m in scenario.mention)
    summary = f"{hit.title} — {hit.root_cause}" if hit else (diagnosis.error or text or "-")
    return Outcome(scenario, [s.kind for s in signals], alert is not None, categories, diagnosed, summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="run one scenario by name")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    real, reason = select_provider()
    print(f"{reason}\n")

    outcomes = []
    for scenario in SCENARIOS:
        if args.only and scenario.name != args.only:
            continue
        print(f"→ {scenario.name}: {scenario.fault} ...", flush=True)
        outcomes.append(run_scenario(real, scenario))

    print("\n| Senaryo | Sinyaller (kod) | Alarm | Teşhis (LLM) | Doğru mu |")
    print("|---|---|---|---|---|")
    for o in outcomes:
        alarm = ("✅" if o.alerted else "❌") if o.scenario.alert else ("—" if not o.alerted else "⚠️ gereksiz")
        print(f"| {o.scenario.name} | {', '.join(o.signals) or '-'} | {alarm} | {', '.join(o.categories) or '-'} | {'✅' if o.diagnosed else '❌'} |")
    print()
    for o in outcomes:
        print(f"• {o.scenario.name}: {o.summary[:300]}")
    correct = sum(o.diagnosed for o in outcomes)
    alerts_ok = sum(o.alerted == o.scenario.alert for o in outcomes)
    print(f"\nTeşhis: {correct}/{len(outcomes)} doğru · Alarm: {alerts_ok}/{len(outcomes)} doğru")
    return 0 if correct >= len(outcomes) - 1 else 1


if __name__ == "__main__":
    sys.exit(main())
