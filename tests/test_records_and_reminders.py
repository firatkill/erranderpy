from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pydantic
import pytest

from erranderpy.core import reminders
from erranderpy.core.dates import parse_datetime
from erranderpy.core.db import Database
from erranderpy.core.records import RecordsSpec, build_records_agent
from erranderpy.core.tools import ToolContext
from erranderpy.evals.scoring import match

TZ = ZoneInfo("Europe/Istanbul")
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=TZ)  # Wednesday

CALENDAR = {
    "name": "calendar",
    "kind": "records",
    "title": "📅 Takvim",
    "item": "etkinlik",
    "description": "Keeps appointments at a date and time; reminds before them.",
    "fields": [
        {"name": "title", "type": "text", "required": True},
        {"name": "when", "type": "datetime", "required": True},
        {"name": "place", "type": "text"},
    ],
    "reminders": [{"field": "when", "before_minutes": 60}],
}


class Calendar:
    def __init__(self, spec=CALENDAR):
        self.db = Database(":memory:")
        self.agent = build_records_agent(RecordsSpec.model_validate(spec))
        self.now = NOW

    def __call__(self, tool, **args):
        ctx = ToolContext(self.db, 1, 1, self.now, self.agent.messages_for("tr"))
        return self.agent.tools[tool].run(ctx, args)

    def pending(self):
        return [dict(r) for r in self.db.query("SELECT due_at, text FROM reminders WHERE status = 'pending'")]


def test_spec_validation_catches_bad_drafts():
    bad = [
        {**CALENDAR, "fields": [{"name": "when", "type": "datetime", "required": True}]},  # no title field
        {**CALENDAR, "reminders": [{"field": "place", "before_minutes": 10}]},  # reminder on text
        {**CALENDAR, "fields": [*CALENDAR["fields"], {"name": "kind", "type": "choice"}]},  # choice w/o options
        {**CALENDAR, "fields": [*CALENDAR["fields"], {"name": "title", "type": "text"}]},  # duplicate name
        {**CALENDAR, "name": "../etc"},  # not a safe name
    ]
    for spec in bad:
        with pytest.raises(pydantic.ValidationError):
            RecordsSpec.model_validate(spec)


def test_generated_tools_and_schema():
    cal = Calendar()
    assert set(cal.agent.tools) == {"add_entry", "list_entries", "update_entry", "delete_entry"}
    schema = cal.agent.tools["add_entry"].spec.parameters
    assert schema["required"] == ["title", "when"]
    assert "HH:MM" in schema["properties"]["when"]["description"]
    assert "ask one short question" in cal.agent.system_prompt


def test_add_list_update_delete_with_reminders():
    cal = Calendar()
    reply = cal("add_entry", title="AVM", when="2026-11-11 14:00", place="Kadıköy").text
    assert reply == "📅 Kaydettim (#1): AVM · 11.11 Çarşamba 14:00 · Kadıköy. ⏰ 11.11 Çarşamba 13:00 hatırlatacağım."
    assert cal.pending() == [{"due_at": "2026-11-11T10:00:00+00:00", "text": "⏰ Hatırlatma: AVM · 11.11 Çarşamba 14:00 · Kadıköy"}]

    assert cal("list_entries").text == "📅 Takvim\n#1 · AVM · 11.11 Çarşamba 14:00 · Kadıköy"
    assert cal("update_entry", entry="avm", when="2026-11-11 15:30").text.startswith("✏️ Güncelledim: AVM · 11.11 Çarşamba 15:30")
    assert [r["due_at"] for r in cal.pending()] == ["2026-11-11T11:30:00+00:00"]  # old reminder cancelled

    assert cal("delete_entry", entry="#1").text == "🗑️ Sildim: AVM · 11.11 Çarşamba 15:30 · Kadıköy."
    assert cal.pending() == []
    assert cal("list_entries").text == "📅 Takvim: kayıt yok."


def test_bad_values_go_back_to_the_model_not_the_user():
    cal = Calendar()
    result = cal("add_entry", title="x", when="yarın")  # a date without a time
    assert result.final is False and "not a valid date-time" in result.text
    assert cal.db.one("SELECT COUNT(*) AS n FROM records")["n"] == 0


def test_no_reminder_in_the_past_and_upcoming_hides_past_entries():
    cal = Calendar()
    reply = cal("add_entry", title="kahve", when="today 10:30").text  # due 09:30, already past
    assert "hatırlatacağım" not in reply and cal.pending() == []
    cal("add_entry", title="eski", when="2026-11-01 10:00")
    assert "eski" not in cal("list_entries").text
    assert "eski" in cal("list_entries", scope="all").text


def test_date_fields_remind_at_nine_minus_offset():
    spec = {
        **CALENDAR,
        "name": "birthdays",
        "fields": [{"name": "person", "type": "text", "required": True}, {"name": "day", "type": "date", "required": True}],
        "reminders": [{"field": "day", "before_minutes": 1440}],
    }
    cal = Calendar(spec)
    cal("add_entry", person="Ayşe", day="2026-11-20")
    assert cal.pending()[0]["due_at"] == "2026-11-19T06:00:00+00:00"  # 19.11 09:00 Istanbul


def test_reminders_are_sent_once_late_ones_marked_old_ones_dropped():
    db = Database(":memory:")
    now = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    reminders.schedule(db, 1, "calendar", now - timedelta(minutes=1), "on time")
    reminders.schedule(db, 1, "calendar", now - timedelta(hours=2), "late")
    reminders.schedule(db, 1, "calendar", now - timedelta(days=1), "too old")
    reminders.schedule(db, 1, "calendar", now + timedelta(minutes=5), "future")
    due = reminders.take_due(db, now)
    assert [(d.text, d.late) for d in due] == [("late", True), ("on time", False)]
    assert reminders.take_due(db, now) == []  # never twice
    statuses = {r["text"]: r["status"] for r in db.query("SELECT text, status FROM reminders")}
    assert statuses == {"on time": "sent", "late": "late", "too old": "missed", "future": "pending"}
    with pytest.raises(ValueError):
        reminders.schedule(db, 1, "x", datetime(2026, 1, 1), "naive")


def test_parse_datetime():
    today = date(2026, 11, 4)
    assert parse_datetime("2026-11-11 14:00", today) == datetime(2026, 11, 11, 14, 0)
    assert parse_datetime("2026-11-11T09:05", today) == datetime(2026, 11, 11, 9, 5)
    assert parse_datetime("tomorrow 9:30", today) == datetime(2026, 11, 5, 9, 30)
    assert parse_datetime("cuma 20.00", today) == datetime(2026, 11, 6, 20, 0)
    for bad in ("2026-11-11", "yarın", "25:00 tomorrow", "tomorrow 25:00"):
        assert parse_datetime(bad, today) is None


def test_has_matcher():
    assert match({"$has": [{"type": "datetime"}]}, [{"type": "text"}, {"type": "datetime", "name": "when"}])
    assert not match({"$has": [{"type": "number"}]}, [{"type": "text"}])
    assert not match({"$has": [{"type": "text"}]}, "not a list")


def test_ambiguous_time_is_asked_by_code_and_nothing_is_saved():
    cal = Calendar()
    result = cal("add_entry", title="AVM", when="2026-11-11 02:00", time_ambiguous=True)
    assert result.final and result.text == "🕑 Saat 2 derken öğleden sonra 14:00 mi, yoksa 02:00 mi?"
    assert cal.db.one("SELECT COUNT(*) AS n FROM records")["n"] == 0
    assert "time_ambiguous" in cal.agent.tools["add_entry"].spec.parameters["properties"]


def test_summary_punctuation_and_ambiguity_label_with_minutes():
    cal = Calendar()
    reply = cal("add_entry", title="Toplantı", when="2026-11-05 14:00", place="Google Meet üzerinden.").text
    assert "Google Meet üzerinden." in reply and ".." not in reply
    question = cal("add_entry", title="x", when="2026-11-05 01:30", time_ambiguous=True).text
    assert question == "🕑 Saat 1.30 derken öğleden sonra 13:30 mi, yoksa 01:30 mi?"


NO_RULES = {**CALENDAR, "reminders": []}


def test_without_rules_the_bot_offers_a_reminder_and_honours_one_per_entry():
    cal = Calendar(NO_RULES)
    reply = cal("add_entry", title="Google Meet toplantısı", when="2026-11-11 15:00").text
    assert reply.endswith('⏰ Hatırlatma ister misin? Örneğin "1 saat önce hatırlat" yazman yeterli.')
    assert cal.pending() == []

    # "1 saat önce hatırlat" -> update_entry with remind_before_minutes
    reply = cal("update_entry", entry="#1", remind_before_minutes=60).text
    assert "⏰ 11.11 Çarşamba 14:00 hatırlatacağım." in reply
    # moving the meeting moves its reminder too: the request is stored in the entry
    cal("update_entry", entry="#1", when="2026-11-08 15:00")
    assert [r["due_at"] for r in cal.pending()] == ["2026-11-08T11:00:00+00:00"]
    assert "_remind_before" not in cal("list_entries").text


def test_asked_reminder_overrides_the_default_rule_and_no_offer_when_rules_exist():
    cal = Calendar()
    reply = cal("add_entry", title="uçuş", when="2026-11-11 15:00", remind_before_minutes=180).text
    assert "11.11 Çarşamba 12:00 hatırlatacağım" in reply and "ister misin" not in reply
    assert len(cal.pending()) == 1  # only the asked one, not also the 60-minute default
    assert "ister misin" not in cal("add_entry", title="x", when="2026-11-12 10:00").text


def test_moving_the_day_keeps_the_time_and_changing_the_time_keeps_the_day():
    cal = Calendar()
    cal("add_entry", title="toplantı", when="2026-11-11 15:00")
    assert "09.11 Pazartesi 15:00" in cal("update_entry", entry="toplantı", when="2026-11-09").text
    assert "09.11 Pazartesi 16:30" in cal("update_entry", entry="toplantı", when="16:30").text
    assert "10.11 Salı 09:00" in cal("update_entry", entry="toplantı", when="2026-11-10 09:00").text


def test_regex_matcher():
    assert match({"when": r"re:\d{4}-11-09( 15:00)?"}, {"when": "2026-11-09"})
    assert match({"when": r"re:\d{4}-11-09( 15:00)?"}, {"when": "2026-11-09 15:00"})
    assert not match({"when": r"re:\d{4}-11-09( 15:00)?"}, {"when": "2026-11-09 00:00"})
