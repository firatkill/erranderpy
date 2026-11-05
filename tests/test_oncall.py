"""The on-call layer: detectors on real pipeline traces, alerts, reports, diagnosis."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from erranderpy.assistant import Assistant
from erranderpy.core import reminders
from erranderpy.core.db import Database
from erranderpy.core.llm import LLMError
from erranderpy.core.registry import load_agents
from erranderpy.oncall.service import Oncall
from erranderpy.oncall.signals import detect, redact
from tests.fakes import ScriptedLLM, route_answer, text_answer, tool_answer

USER = CHAT = 1001
TZ = ZoneInfo("Europe/Istanbul")


def setup(bot_answers, oncall_answers=()):
    db = Database(":memory:")
    agents = load_agents(db=db)
    assistant = Assistant(ScriptedLLM(bot_answers), agents, db, TZ, "tr")
    oncall_llm = ScriptedLLM(list(oncall_answers))
    oncall = Oncall(oncall_llm, db, lambda: assistant.agents, "tr", env={"ONCALL_CANARY": "off"})
    return assistant, oncall, oncall_llm, db


def kinds(db):
    now = datetime.now(UTC)
    return {s.kind: s for s in detect(db, now - timedelta(hours=1), now + timedelta(seconds=1))}


def test_a_bad_key_is_an_alert_written_by_code_without_asking_the_llm():
    auth = LLMError("auth", "Incorrect API key provided: sk-proj-abcdef123456", "openai")
    assistant, oncall, oncall_llm, db = setup([auth, auth, auth])
    for text in ("süt al", "ekmek al", "selam"):
        assistant.handle(USER, CHAT, text)
    signal = kinds(db)["llm_auth"]
    assert signal.severity == "critical" and signal.count == 3
    assert "sk-proj" not in " ".join(signal.examples)  # keys never leave the database

    alert = oncall.check()
    assert alert.startswith("🚨 Oncall uyarısı") and "API anahtarını reddediyor (3 mesaj" in alert
    assert oncall_llm.calls == []  # the LLM is the problem: don't ask it
    assert oncall.check() is None  # not again within the cooldown


def test_a_tool_exception_is_traced_to_the_tool_and_diagnosed():
    assistant, oncall, oncall_llm, db = setup(
        [route_answer(("shopping", "süt al")), tool_answer(("add_items", {"items": [{"name": "süt"}]}))],
        [
            tool_answer(("get_trace", {"trace_id": "x"})),
            tool_answer(("submit_report", {"findings": [{
                "category": "tool_bug", "severity": "critical", "title": "add_items çöküyor",
                "root_cause": "shopping/tools.py add_items RuntimeError", "suggestion": "Hatayı düzelt", "evidence": ["t1"],
            }]})),
        ],
    )
    tool = assistant.agents["shopping"].tools["add_items"]

    def broken(ctx, **kwargs):
        raise RuntimeError("no such column: quantity")

    assistant.agents["shopping"].tools["add_items"] = type(tool)(tool.name, tool.description, broken, tool.args_model, tool.spec)
    assert "ters gitti" in assistant.handle(USER, CHAT, "süt al")

    signal = kinds(db)["internal_error"]
    assert "tool.call.shopping.add_items: RuntimeError: no such column" in signal.examples[0]
    alert = oncall.check()
    assert "🐞 Kodda hata" in alert and "🔴 add_items çöküyor" in alert and "Öneri: Hatayı düzelt" in alert
    assert "internal_error x1" in oncall_llm.calls[0]["messages"][0].text


def test_overdue_reminders_mean_the_scheduler_is_not_running():
    _, oncall, _, db = setup([])
    reminders.schedule(db, USER, "calendar", datetime.now(TZ) - timedelta(minutes=20), "⏰ Toplantı", 1)
    assert kinds(db)["scheduler"].severity == "critical"
    assert "1 hatırlatmanın vakti geçti" in oncall.check()


def test_semantic_signals_unknown_routes_doubt_and_repeats():
    assistant, _, _, db = setup(
        [
            route_answer(("unknown", "8 kasıma toplantı koydun mu?")), text_answer("Bilmiyorum."),
            route_answer(("unknown", "8 kasıma toplantı koydun mu?")), text_answer("Bilmiyorum."),
        ]
    )
    assistant.handle(USER, CHAT, "8 kasıma toplantı koydun mu?")
    assistant.handle(USER, CHAT, "8 kasıma toplantı koydun mu?")
    found = kinds(db)
    assert found["unknown_routes"].count == 2
    assert 'after bot "Bilmiyorum."' in found["user_doubt"].examples[-1]
    assert found["repeated_message"].count == 1


def test_report_with_nothing_wrong_and_oncall_spans_are_not_watched():
    assistant, oncall, _, db = setup(
        [route_answer(("shopping", "süt al")), tool_answer(("add_items", {"items": [{"name": "süt"}]}))],
    )
    assistant.handle(USER, CHAT, "süt al")
    db.execute(
        "INSERT INTO traces (trace_id, step, agent, error, created_at) VALUES ('o1', 'agent.llm', 'oncall', 'x', ?)",
        (datetime.now(UTC).isoformat(timespec="seconds"),),
    )
    report = oncall.report()
    assert report.startswith("🩺 Oncall raporu · son 24 saat")
    assert "1 mesaj · 2 LLM çağrısı" in report and report.endswith("✅ Sorun görmedim.")


def test_diagnosis_without_a_report_still_lists_the_signals():
    assistant, oncall, _, db = setup(
        [route_answer(("unknown", "a")), text_answer("?")] * 3,
        [text_answer("Bence sorun yok.")],
    )
    for text in ("a", "b", "c"):
        assistant.handle(USER, CHAT, text)
    report = oncall.report()
    assert "(Teşhis yapılamadı: no report.)" in report and "unknown_routes ×3" in report


def test_redact_hides_keys_and_tokens():
    text = redact("key sk-ant-api03-abcdefgh and AIzaSyA1234567890abc and 123456789:AAH" + "x" * 33)
    assert text == "key [redacted] and [redacted] and [redacted]"


class RouteEverythingTo:
    """A router that sends every message to one agent (a broken description's effect)."""

    name, model = "fake", "fake-1"

    def __init__(self, agent):
        self.agent = agent

    def complete(self, system, messages, tools=None, **kwargs):
        return route_answer((self.agent, messages[-1].text))


def test_canary_turns_a_silent_misroute_into_a_signal():
    # Chaos test, 2026-11-05: a broken shopping description sent shopping messages to
    # tasks. No error anywhere; only the eval knows where they belong.
    from erranderpy.oncall.canary import router_canary

    agents = load_agents(db=Database(":memory:"))
    signal = router_canary(RouteEverythingTo("tasks"), agents)
    assert signal.kind == "eval_regression" and signal.count >= 10
    assert any("expected ['shopping'], got ['tasks']" in e for e in signal.examples)
