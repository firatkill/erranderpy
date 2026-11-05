"""The whole pipeline (router → agent → tools → reply → history → traces) with a scripted LLM."""

from zoneinfo import ZoneInfo

import pytest

from erranderpy.assistant import Assistant
from erranderpy.core.db import Database
from erranderpy.core.llm import LLMError
from erranderpy.core.registry import load_agents
from erranderpy.core.tracing import load_trace
from tests.fakes import ScriptedLLM, route_answer, text_answer, tool_answer

CHAT = USER = 1001


def make(answers):
    db = Database(":memory:")
    llm = ScriptedLLM(answers)
    assistant = Assistant(llm, load_agents(db=db), db, ZoneInfo("Europe/Istanbul"), "tr")
    return assistant, llm, db


def test_add_item_takes_two_llm_calls_and_code_writes_the_reply():
    assistant, llm, db = make(
        [
            route_answer(("shopping", "zeytinyağı almam lazım")),
            tool_answer(("add_items", {"items": [{"name": "zeytinyağı"}]})),
        ]
    )
    reply = assistant.handle(USER, CHAT, "zeytinyağı almam lazım")
    assert reply == "🛒 Ekledim: zeytinyağı. Listede 1 ürün var."
    assert len(llm.calls) == 2  # router + agent; no third call to rephrase (early exit)

    history = db.recent_messages(CHAT, 10)
    assert [(r["role"], r["text"]) for r in history] == [("user", "zeytinyağı almam lazım"), ("assistant", reply)]
    steps = [s["step"] for s in load_trace(db, history[0]["trace_id"])]
    assert steps == ["router.decide", "agent.llm", "tool.call", "message"]


def test_two_requests_in_one_message():
    assistant, llm, _ = make(
        [
            route_answer(("shopping", "süt al"), ("unknown", "selam")),
            tool_answer(("add_items", {"items": [{"name": "süt"}]})),
            text_answer("Merhaba!"),  # fallback for the unknown part
        ]
    )
    reply = assistant.handle(USER, CHAT, "selam, süt al")
    assert reply == "🛒 Ekledim: süt. Listede 1 ürün var.\n\nMerhaba!"


def test_history_is_sent_as_context():
    assistant, llm, _ = make(
        [
            route_answer(("shopping", "süt al")),
            tool_answer(("add_items", {"items": [{"name": "süt"}]})),
            route_answer(("shopping", "bir de ekmek")),
            tool_answer(("add_items", {"items": [{"name": "ekmek"}]})),
        ]
    )
    assistant.handle(USER, CHAT, "süt al")
    assistant.handle(USER, CHAT, "bir de ekmek")
    router_messages = llm.calls[2]["messages"]
    assert [m.text for m in router_messages] == ["süt al", "🛒 Ekledim: süt. Listede 1 ürün var.", "bir de ekmek"]


def test_unknown_tool_gets_a_second_chance():
    assistant, llm, _ = make(
        [
            route_answer(("shopping", "süt al")),
            tool_answer(("add_milk", {})),
            tool_answer(("add_items", {"items": [{"name": "süt"}]})),
        ]
    )
    assert assistant.handle(USER, CHAT, "süt al").startswith("🛒 Ekledim: süt")
    assert "no tool named 'add_milk'" in llm.calls[2]["messages"][-1].text


def test_model_answering_in_text_is_passed_through():
    assistant, _, _ = make([route_answer(("shopping", "şey al")), text_answer("Ne almamı istersin?")])
    assert assistant.handle(USER, CHAT, "şey al") == "Ne almamı istersin?"


def test_router_without_tool_call_falls_back_to_unknown():
    assistant, _, db = make([text_answer("shopping"), text_answer("Selam! Alışveriş listeni yönetebilirim.")])
    assert assistant.handle(USER, CHAT, "selam") == "Selam! Alışveriş listeni yönetebilirim."
    trace_id = db.recent_messages(CHAT, 1)[0]["trace_id"]
    router = next(s for s in load_trace(db, trace_id) if s["step"] == "router.decide")
    assert router["error"] == "no_tool_call"


@pytest.mark.parametrize(("kind", "expected"), [("timeout", "(timeout)"), ("auth", "make doctor")])
def test_llm_errors_become_friendly_replies(kind, expected):
    assistant, _, db = make([LLMError(kind, "boom", "scripted")])
    reply = assistant.handle(USER, CHAT, "süt al")
    assert expected in reply
    trace_id = db.recent_messages(CHAT, 1)[0]["trace_id"]
    assert load_trace(db, trace_id)[-1]["error"].startswith("LLMError")


def test_status_and_trace_commands():
    assistant, _, _ = make(
        [route_answer(("shopping", "süt al")), tool_answer(("add_items", {"items": [{"name": "süt"}]}))]
    )
    assert "Henüz" in assistant.last_trace_text(CHAT)
    assistant.handle(USER, CHAT, "süt al")
    assert "1 mesaj, 2 LLM çağrısı" in assistant.status_text()
    trace = assistant.last_trace_text(CHAT)
    assert "router.decide" in trace and "→ shopping: süt al" in trace
    assert "tool.call [shopping] add_items" in trace


def test_routes_for_the_same_agent_are_merged():
    from erranderpy.core.router import Route, parse_routes

    result = route_answer(("shopping", "2 paket makarna"), ("unknown", "selam"), ("shopping", "süt"))
    assert parse_routes(result, ["shopping"], "x") == [
        Route("shopping", "2 paket makarna, süt", ("2 paket makarna", "süt")),
        Route("unknown", "selam", ("selam",)),
    ]


def test_scheduled_job_runs_under_the_assistant_and_lands_in_history():
    assistant, _, db = make([])
    job = next(j for j in assistant.agents["tasks"].jobs if j.name == "evening_summary")
    assert assistant.run_job(job, USER, CHAT) is None  # empty day, nothing sent
    db.execute(
        "INSERT INTO tasks (user_id, day, title, title_key, created_at) VALUES (?, date('now'), 'fatura', 'fatura', '')",
        (USER,),
    )
    # The task above is stored with SQLite's UTC date; the job uses the user's local date.
    # Re-insert for the local day to be safe around midnight.
    from datetime import datetime

    today = datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()
    db.execute("UPDATE tasks SET day = ?", (today,))
    text = assistant.run_job(job, USER, CHAT)
    assert text.startswith("🌙 Günün özeti")
    assert db.recent_messages(CHAT, 1)[0]["text"] == text
    trace_id = db.recent_messages(CHAT, 1)[0]["trace_id"]
    assert [s["step"] for s in load_trace(db, trace_id)] == ["job.run"]


def test_a_dropped_request_gets_one_nudge():
    # Live, 2026-11-04 20:57: "add the 14th. also a reminder for the 8th" did only the first.
    from erranderpy.core.tracing import load_trace

    assistant, llm, db = make(
        [
            route_answer(("shopping", "süt al"), ("shopping", "ekmeği aldım")),
            tool_answer(("add_items", {"items": [{"name": "süt"}]})),
            tool_answer(("add_items", {"items": [{"name": "ekmek"}]})),
        ]
    )
    reply = assistant.handle(USER, CHAT, "süt al. ekmeği de ekle")
    assert reply.count("🛒 Ekledim") == 2
    assert "2 requests for you" in llm.calls[1]["system"]
    assert "[system check] You made 1 tool call(s) for 2 requests" in llm.calls[2]["messages"][-1].text
    steps = [s["step"] for s in load_trace(db, db.recent_messages(CHAT, 1)[0]["trace_id"])]
    assert "agent.nudge" in steps


def test_nudge_answered_with_done_keeps_the_first_reply():
    assistant, llm, _ = make(
        [
            route_answer(("shopping", "2 paket makarna"), ("shopping", "süt")),
            tool_answer(("add_items", {"items": [{"name": "makarna", "quantity": 2}, {"name": "süt"}]})),
            text_answer("DONE"),
        ]
    )
    assert assistant.handle(USER, CHAT, "2 paket makarna, bir de süt") == "🛒 Ekledim: 2 × makarna, süt. Listede 2 ürün var."
    assert len(llm.calls) == 3  # one extra call only because 1 call < 2 requests


def test_no_nudge_when_calls_match_requests():
    assistant, llm, _ = make([route_answer(("shopping", "süt al")), tool_answer(("add_items", {"items": [{"name": "süt"}]}))])
    assistant.handle(USER, CHAT, "süt al")
    assert len(llm.calls) == 2


def test_a_repeated_call_after_the_nudge_is_not_run_twice():
    assistant, _, db = make(
        [
            route_answer(("shopping", "zeytinyağı al"), ("shopping", "2 paket makarna")),
            tool_answer(("add_items", {"items": [{"name": "zeytinyağı"}, {"name": "makarna", "quantity": 2}]})),
            tool_answer(("add_items", {"items": [{"name": "zeytinyağı"}, {"name": "makarna", "quantity": 2}]})),
            text_answer("DONE"),
        ]
    )
    reply = assistant.handle(USER, CHAT, "zeytinyağı al, bir de 2 paket makarna")
    assert reply == "🛒 Ekledim: zeytinyağı, 2 × makarna. Listede 2 ürün var."
    assert db.one("SELECT COUNT(*) AS n FROM traces WHERE step = 'tool.call'")["n"] == 1


def test_records_agent_sees_its_entries_in_the_prompt():
    from erranderpy.core.records import RecordsSpec, build_records_agent

    spec = {
        "name": "calendar", "kind": "records", "title": "📅 Takvim", "item": "etkinlik",
        "description": "Keeps events at a date and time.",
        "fields": [{"name": "title", "type": "text", "required": True}, {"name": "when", "type": "datetime", "required": True}],
    }
    assistant, llm, _ = make(
        [
            route_answer(("calendar", "30 aralık 10:00 dişçi")),
            tool_answer(("add_entry", {"title": "Dişçi", "when": "2099-12-30 10:00"})),
            route_answer(("calendar", "dişçiyi 11'e al")),
            tool_answer(("update_entry", {"entry": "#1", "when": "11:00"})),
        ]
    )
    assistant.agents["calendar"] = build_records_agent(RecordsSpec.model_validate(spec))
    assistant.handle(USER, CHAT, "30 aralık 10:00 dişçi")
    assistant.handle(USER, CHAT, "dişçiyi 11'e al")
    assert "no upcoming entries" in llm.calls[1]["system"]
    assert "#1 · Dişçi · 30.12" in llm.calls[3]["system"]
