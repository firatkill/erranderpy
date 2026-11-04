"""The bot adds an ability to itself: propose → confirm → create → reload → replay."""

from zoneinfo import ZoneInfo

import pytest

from erranderpy.assistant import Assistant
from erranderpy.core.db import Database
from erranderpy.core.registry import AGENTS_DIR, load_agents
from tests.fakes import ScriptedLLM, route_answer, text_answer, tool_answer

USER = CHAT = 5

DRAFT = {
    "name": "calendar",
    "title": "📅 Takvim",
    "item": "etkinlik",
    "description": "Keeps appointments at a date and clock time and reminds before them.",
    "examples": ["cuma 10:30 toplantı"],
    "fields": [
        {"name": "title", "type": "text", "label": "başlık", "required": True},
        {"name": "when", "type": "datetime", "label": "zaman", "required": True},
    ],
    "reminders": [{"field": "when", "before_minutes": 60}],
}


def make(tmp_path, answers, dirs=None):
    db = Database(":memory:")
    dirs = dirs or [AGENTS_DIR, tmp_path]
    llm = ScriptedLLM(answers)
    return Assistant(llm, load_agents(dirs, db), db, ZoneInfo("Europe/Istanbul"), "tr", dirs), llm, db


def test_full_flow_creates_agent_and_handles_the_original_request(tmp_path):
    assistant, llm, db = make(
        tmp_path,
        [
            # 1) "11 kasım 14:00 avm" -> builder proposes, nothing created yet
            route_answer(("builder", "11 kasımda 14:00'te avm'ye gitmem gerek")),
            tool_answer(("propose_agent", {"draft": DRAFT, "handle_after_creation": True})),
            # 2) "evet" -> create, reload, then the original request is routed again
            route_answer(("builder", "evet")),
            tool_answer(("create_agent", {})),
            route_answer(("calendar", "11 kasımda 14:00'te avm'ye gitmem gerek")),
            tool_answer(("add_entry", {"title": "AVM", "when": "2026-11-11 14:00"})),
        ],
    )
    preview = assistant.handle(USER, CHAT, "11 kasımda 14:00'te avm'ye gitmem gerek")
    assert preview.startswith("🧩 Yeni yetenek önerisi: 📅 Takvim")
    assert "başlık (metin, zorunlu) · zaman (tarih-saat, zorunlu)" in preview
    assert "⏰ zaman gelmeden 1 saat önce hatırlatma" in preview
    assert not (tmp_path / "calendar").exists()

    reply = assistant.handle(USER, CHAT, "evet")
    assert reply.startswith("✅ 📅 Takvim eklendi!")
    assert "📅 Kaydettim (#1): AVM · 11.11 Çarşamba 14:00." in reply
    assert (tmp_path / "calendar" / "agent.yaml").exists()
    assert assistant.agents["calendar"].created_by == "builder"
    assert "calendar" in assistant.agents and list(assistant.agents)[-1] == "builder"  # builder stays last
    assert "senin için eklendi" in assistant.agents_text()
    # the request was replayed in the user's own words, not the model's
    replay_router = llm.calls[4]["messages"][-1].text
    assert replay_router == "11 kasımda 14:00'te avm'ye gitmem gerek"
    # the builder saw the existing agents in its prompt
    assert "Agents that already exist" in llm.calls[1]["system"] and "- shopping" in llm.calls[1]["system"]


def test_invalid_draft_goes_back_to_the_model(tmp_path):
    broken = {**DRAFT, "fields": [{"name": "when", "type": "datetime", "required": True}]}  # no title field
    assistant, llm, _ = make(
        tmp_path,
        [
            route_answer(("builder", "takvim tut")),
            tool_answer(("propose_agent", {"draft": broken})),
            tool_answer(("propose_agent", {"draft": DRAFT})),
        ],
    )
    assert assistant.handle(USER, CHAT, "takvim tut").startswith("🧩 Yeni yetenek önerisi")
    assert "draft is invalid" in llm.calls[2]["messages"][-1].text


def test_existing_names_and_missing_proposal_are_refused(tmp_path):
    shopping_clash = {**DRAFT, "name": "shopping"}
    assistant, llm, _ = make(
        tmp_path,
        [
            route_answer(("builder", "x")),
            tool_answer(("propose_agent", {"draft": shopping_clash})),
            text_answer("Alışveriş listen zaten var, doğrudan yazabilirsin."),
            route_answer(("builder", "evet")),
            tool_answer(("create_agent", {})),
            text_answer("Önce ne eklemek istediğini söyler misin?"),
        ],
    )
    assert assistant.handle(USER, CHAT, "x") == "Alışveriş listen zaten var, doğrudan yazabilirsin."
    assert "already exists" in llm.calls[2]["messages"][-1].text
    assert assistant.handle(USER, CHAT, "evet") == "Önce ne eklemek istediğini söyler misin?"
    assert "no pending proposal" in llm.calls[5]["messages"][-1].text


def test_cancel_and_delete(tmp_path):
    assistant, _, _ = make(
        tmp_path,
        [
            route_answer(("builder", "takvim")),
            tool_answer(("propose_agent", {"draft": DRAFT})),
            route_answer(("builder", "vazgeçtim")),
            tool_answer(("cancel_proposal", {})),
            route_answer(("builder", "takvim")),
            tool_answer(("propose_agent", {"draft": DRAFT})),
            route_answer(("builder", "evet")),
            tool_answer(("create_agent", {})),
            route_answer(("builder", "shopping'i kaldır")),
            tool_answer(("delete_agent", {"name": "shopping"})),
            route_answer(("builder", "takvimi kaldır")),
            tool_answer(("delete_agent", {"name": "calendar"})),
        ],
    )
    assistant.handle(USER, CHAT, "takvim")
    assert assistant.handle(USER, CHAT, "vazgeçtim") == "Tamam, eklemedim."
    assistant.handle(USER, CHAT, "takvim")
    assistant.handle(USER, CHAT, "evet")  # no pending_request: nothing to replay
    assert "calendar" in assistant.agents
    assert "benim eklediğim bir yetenek değil" in assistant.handle(USER, CHAT, "shopping'i kaldır")
    assert assistant.handle(USER, CHAT, "takvimi kaldır").startswith("🗑️ 📅 Takvim kaldırıldı")
    assert "calendar" not in assistant.agents
    assert not (tmp_path / "calendar").exists()
    assert any((tmp_path / "_deleted").iterdir())  # moved aside, not deleted


def test_without_a_user_folder_the_builder_writes_nothing(tmp_path):
    assistant, _, _ = make(
        tmp_path,
        [route_answer(("builder", "takvim")), tool_answer(("propose_agent", {"draft": DRAFT}))],
        dirs=[AGENTS_DIR],
    )
    assert assistant.handle(USER, CHAT, "takvim") == "Bu kurulumda yeni yetenek ekleme kapalı."


def test_capabilities_mention_scheduled_jobs(tmp_path):
    assistant, _, _ = make(tmp_path, [])
    caps = assistant.capabilities()
    assert "Automatically every day at 21:00" in caps
    assert "🧩 Yetenek ekleme" in caps


@pytest.mark.parametrize("folder", ["agents"])
def test_built_in_agents_never_include_user_ones(folder):
    # the repo's agents/ folder must not contain agents the bot created
    assert all(a.created_by != "builder" for a in load_agents(AGENTS_DIR).values())


NO_REMINDERS = {**DRAFT, "reminders": []}


def test_clock_fields_get_a_default_reminder_unless_refused(tmp_path):
    assistant, _, _ = make(
        tmp_path,
        [
            route_answer(("builder", "takvim")),
            tool_answer(("propose_agent", {"draft": NO_REMINDERS})),
            route_answer(("builder", "hatırlatma istemem")),
            tool_answer(("propose_agent", {"draft": NO_REMINDERS, "without_reminders": True})),
        ],
    )
    assert "⏰ zaman gelmeden 1 saat önce hatırlatma" in assistant.handle(USER, CHAT, "takvim")
    assert "⏰" not in assistant.handle(USER, CHAT, "hatırlatma istemem")


def test_reproposing_the_same_draft_is_turned_into_a_hint(tmp_path):
    # Live, 2026-11-04: the user said "Ekle" and the model proposed the same thing again.
    assistant, llm, _ = make(
        tmp_path,
        [
            route_answer(("builder", "takvim")),
            tool_answer(("propose_agent", {"draft": DRAFT})),
            route_answer(("builder", "Ekle")),
            tool_answer(("propose_agent", {"draft": {**DRAFT, "description": "Slightly different wording here."}})),
            tool_answer(("create_agent", {})),
        ],
    )
    assistant.handle(USER, CHAT, "takvim")
    assert assistant.handle(USER, CHAT, "Ekle").startswith("✅ 📅 Takvim eklendi!")
    assert "identical to the proposal" in llm.calls[4]["messages"][-1].text


def test_updating_an_ability_keeps_entries_and_adds_reminders_to_them(tmp_path):
    assistant, _, db = make(
        tmp_path,
        [
            route_answer(("builder", "takvim")),
            tool_answer(("propose_agent", {"draft": NO_REMINDERS, "without_reminders": True})),
            route_answer(("builder", "evet")),
            tool_answer(("create_agent", {})),
            route_answer(("calendar", "toplantı")),
            tool_answer(("add_entry", {"title": "Toplantı", "when": "2099-11-05 14:00"})),
            route_answer(("builder", "takvime 1 saat önce hatırlatma ekle")),
            tool_answer(("propose_agent", {"draft": DRAFT})),
            route_answer(("builder", "evet")),
            tool_answer(("create_agent", {})),
        ],
    )
    assistant.handle(USER, CHAT, "takvim")
    assistant.handle(USER, CHAT, "evet")
    assert "hatırlatacağım" not in assistant.handle(USER, CHAT, "toplantı")
    preview = assistant.handle(USER, CHAT, "takvime 1 saat önce hatırlatma ekle")
    assert preview.startswith("🧩 Güncelleme önerisi: 📅 Takvim")
    assert assistant.handle(USER, CHAT, "evet") == "✅ 📅 Takvim güncellendi. Bekleyen hatırlatma sayısı: 1."
    assert assistant.agents["calendar"].reminder_hint  # reloaded with the new rule
    assert db.one("SELECT COUNT(*) AS n FROM records")["n"] == 1  # the entry is still there
    assert any(p.name.endswith("before-update") for p in (tmp_path / "_deleted").iterdir())


def test_single_route_keeps_the_users_own_words(tmp_path):
    from erranderpy.core.router import Route, parse_routes

    result = route_answer(("calendar", "Takvimdeki toplantıyı güncelle: saati 14.00 olsun"))
    original = "Günceller misin, saat 14.00 oldu."
    routes = parse_routes(result, ["calendar"], original)
    assert [(r.agent, r.text) for r in routes] == [("calendar", original)]
