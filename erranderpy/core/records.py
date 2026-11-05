"""Declarative agents: an agent described in YAML, with no code of its own.

A "records" agent keeps a list of entries with typed fields and can remind the
user before a date/time field. From the spec the platform builds the four
tools (add/list/update/delete), their JSON schemas, the system prompt and the
replies. This is what lets the bot create new abilities for itself safely:
the builder agent only writes a validated spec; every line of code that runs
is code we wrote and tested. See ADR-0012 in AGENTS.md.

    name: calendar
    kind: records
    title: "📅 Takvim"
    item: etkinlik
    description: Keeps appointments and events with a date and time; reminds before them.
    fields:
      - {name: title, type: text, required: true, description: what the event is}
      - {name: when,  type: datetime, required: true}
      - {name: place, type: text}
    reminders:
      - {field: when, before_minutes: 60}
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import pydantic
from pydantic import BaseModel, Field, model_validator

from erranderpy.core import reminders
from erranderpy.core.agent import Agent
from erranderpy.core.dates import format_datetime, format_day, parse_datetime, parse_time, resolve_day
from erranderpy.core.db import utc_now_iso
from erranderpy.core.llm import ToolSpec
from erranderpy.core.text import find_unique
from erranderpy.core.tools import Tool, ToolContext, ToolResult, clean_schema

NAME_PATTERN = r"^[a-z][a-z0-9_]{1,30}$"
FieldType = Literal["text", "number", "date", "datetime", "choice"]


class FieldSpec(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, description="lowercase identifier, e.g. 'when'")
    type: FieldType
    label: str = Field(default="", description="name shown to the user, in their language, e.g. 'yer'")
    required: bool = False
    description: str = ""
    options: list[str] | None = Field(default=None, description="allowed values, only for type 'choice'")

    @model_validator(mode="after")
    def _choice_needs_options(self) -> FieldSpec:
        if self.type == "choice" and not self.options:
            raise ValueError(f"field '{self.name}': type 'choice' needs options")
        return self


class ReminderSpec(BaseModel):
    field: str = Field(description="name of a date or datetime field")
    before_minutes: int = Field(ge=0, le=60 * 24 * 14, description="how long before; for a date field, before 09:00")


class RecordsSpec(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, description="lowercase identifier, e.g. 'calendar'")
    kind: Literal["records"] = "records"
    title: str = Field(min_length=1, max_length=40, description="short name shown to the user, with an emoji")
    item: str = Field(min_length=1, max_length=30, description="what one entry is called, in the user's language")
    description: str = Field(min_length=10, max_length=300, description="what it does, for the router (English)")
    examples: list[str] = Field(default_factory=list, max_length=6)
    fields: list[FieldSpec] = Field(min_length=1, max_length=10)
    reminders: list[ReminderSpec] = Field(default_factory=list, max_length=3)
    created_by: str | None = None

    @model_validator(mode="after")
    def _check(self) -> RecordsSpec:
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("field names must be unique")
        if not any(f.type == "text" and f.required for f in self.fields):
            raise ValueError("at least one required text field is needed (the entry's title)")
        by_name = {f.name: f for f in self.fields}
        for r in self.reminders:
            if r.field not in by_name or by_name[r.field].type not in ("date", "datetime"):
                raise ValueError(f"reminder field '{r.field}' must be a date or datetime field")
        return self

    @property
    def title_field(self) -> str:
        return next(f.name for f in self.fields if f.type == "text" and f.required)

    @property
    def time_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.type in ("date", "datetime")]


# --- messages ------------------------------------------------------------------

RECORDS_MESSAGES = {
    "tr": {
        "added": "{emoji} Kaydettim (#{id}): {summary}.",
        "updated": "✏️ Güncelledim: {summary}.",
        "deleted": "🗑️ Sildim: {summary}.",
        "not_found": "\"{entry}\" diye bir kayıt bulamadım.",
        "remind_at": "⏰ {at} hatırlatacağım.",
        "list_header": "{title}",
        "list_empty": "{title}: kayıt yok.",
        "reminder": "⏰ Hatırlatma: {summary}",
        "ambiguous_time": "🕑 Saat {hour} derken öğleden sonra {pm} mi, yoksa {am} mi?",
        "ask_reminder": "⏰ Hatırlatma ister misin? Örneğin \"1 saat önce hatırlat\" yazman yeterli.",
        "weekdays": "Pazartesi,Salı,Çarşamba,Perşembe,Cuma,Cumartesi,Pazar",
    },
    "en": {
        "added": "{emoji} Saved (#{id}): {summary}.",
        "updated": "✏️ Updated: {summary}.",
        "deleted": "🗑️ Deleted: {summary}.",
        "not_found": "I couldn't find an entry \"{entry}\".",
        "remind_at": "⏰ I'll remind you at {at}.",
        "list_header": "{title}",
        "list_empty": "{title}: no entries.",
        "reminder": "⏰ Reminder: {summary}",
        "ambiguous_time": "🕑 By {hour}, do you mean {pm} in the afternoon, or {am}?",
        "ask_reminder": "⏰ Want a reminder? Just write, for example, \"remind me an hour before\".",
        "weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday",
    },
}

# A reminder the user asked for on one entry is stored inside the entry, so it
# survives updates (moving the meeting moves its reminder). Not a field: hidden.
REMIND_KEY = "_remind_before"

DATE_HELP = "'today', 'tomorrow', an English weekday name, or YYYY-MM-DD"
DATETIME_HELP = "date and 24-hour time: 'YYYY-MM-DD HH:MM' (or 'tomorrow HH:MM', 'thursday HH:MM')"


# --- building the agent ----------------------------------------------------------


def build_records_agent(spec: RecordsSpec, path: Path | None = None) -> Agent:
    tools = {t.name: t for t in _make_tools(spec)}
    hint = ", ".join(f"reminds {r.before_minutes} min before '{r.field}'" for r in spec.reminders)
    return Agent(
        name=spec.name,
        description=spec.description,
        examples=spec.examples,
        system_prompt=_system_prompt(spec),
        tools=tools,
        messages=RECORDS_MESSAGES,
        path=path,
        kind="records",
        title=spec.title,
        created_by=spec.created_by,
        reminder_hint=hint,
        live_context=lambda ctx: _entries_context(spec, ctx),
    )


# How many entries the prompt shows. A personal list is short; past this the
# model can still search with list_entries(query=...).
CONTEXT_ENTRIES = 30


def _entries_context(spec: RecordsSpec, ctx: ToolContext) -> str:
    """The entries that exist right now, with their #numbers, for the prompt.

    Without this the agent only knew entries still visible in the last few
    chat messages. Live (2026-11-05): "8 kasımdaki toplantı için 1 saat önce
    hatırlat", a day after saving it, became a *new* entry at 00:00. The prompt
    forbids listing first (listing ends the turn), so the model must already
    see what exists. Read fresh on every turn: after an update it is current.
    """
    rows = _rows(spec, ctx)
    if spec.time_fields:
        start = ctx.today_start_utc()
        rows = [r for r in rows if r["sort_at"] is None or r["sort_at"] >= start]
        heading = "upcoming entries"
    else:
        heading = "entries"
    if not rows:
        return f"\n\nThe user has no {heading} yet."
    lines = []
    for r in rows[:CONTEXT_ENTRIES]:
        data = _data(r)
        line = f"#{r['id']} · {_summary(spec, data, ctx)}"
        if data.get(REMIND_KEY) is not None:
            line += f" · reminder {data[REMIND_KEY]} min before"
        lines.append(line)
    if len(rows) > CONTEXT_ENTRIES:
        lines.append(f"... and {len(rows) - CONTEXT_ENTRIES} more (find them with list_entries and a query)")
    listing = "\n".join(lines)
    return (
        f"\n\nThe user's {heading} (to change or delete one, pass its #number):\n{listing}\n"
        "A request about one of these (\"8 kasımdaki toplantı\") is update_entry or delete_entry on it, "
        "never a new entry. If several could match, ask which one."
    )


def _system_prompt(spec: RecordsSpec) -> str:
    lines = [f"You keep the user's {spec.title} — a list of {spec.item} entries. Fields:"]
    for f in spec.fields:
        extra = f" (one of: {', '.join(f.options or [])})" if f.type == "choice" else ""
        need = "required" if f.required else "optional"
        lines.append(f"- {f.name}: {f.type}, {need}{extra}. {f.description}".rstrip())
    if spec.time_fields:
        if spec.reminders:
            rules = ", ".join(f"{r.before_minutes} min before '{r.field}'" for r in spec.reminders)
            lines.append(
                f"Every entry gets a reminder automatically ({rules}). If the user wants a different "
                "reminder for one entry, set remind_before_minutes."
            )
        else:
            lines.append(
                "This ability has no automatic reminders. If the user asks to be reminded about an entry, "
                "set remind_before_minutes (60 if they didn't say how long before). After saving, the user may "
                "answer \"1 saat önce hatırlat\": that is update_entry on the entry just saved, with remind_before_minutes."
            )
        lines.append("Never write the user's instructions (e.g. 'hatırlatma', 'ekle') into fields.")
    if any(f.type == "datetime" for f in spec.fields):
        lines.append(
            "Clock times: if the user's hour could mean two times (\"saat 2\" = 02:00 or 14:00) and they gave no "
            "hint like sabah/öğleden sonra/akşam/gece, still call the tool with your best guess and set "
            "time_ambiguous=true: the tool then asks the user instead of saving. \"akşam 8\" is 20:00, not ambiguous."
        )
    lines += [
        "Tools: add_entry saves a new entry, list_entries shows them to the user, update_entry changes one, "
        "delete_entry removes one or more (all of them in one call).",
        "To change or delete an entry, call update_entry / delete_entry directly with its #number (from the "
        "entries at the end of this prompt). Never call list_entries first: listing ends your turn.",
        "When updating a date-time, send only what changes: just the date (YYYY-MM-DD) to move it to another day "
        "(the time is kept), just HH:MM to change the time (the day is kept).",
        f"Dates are {DATE_HELP}. Date-times are {DATETIME_HELP}.",
        "If a required field is missing, ask one short question and call no tool in that reply. "
        "Never invent values the user didn't give.",
    ]
    return "\n".join(lines)


def _python_type(f: FieldSpec) -> Any:
    if f.type == "number":
        return float
    if f.type == "choice":
        return Literal[tuple(f.options or [])]  # type: ignore[misc]
    return str


def _field_help(f: FieldSpec) -> str:
    base = {"date": DATE_HELP, "datetime": DATETIME_HELP}.get(f.type, "")
    return " ".join(p for p in (f.description, base) if p)


def _args_model(name: str, spec: RecordsSpec, all_optional: bool, extra: dict[str, Any] | None = None):
    fields: dict[str, Any] = dict(extra or {})
    for f in spec.fields:
        help_text = _field_help(f)
        typ = Annotated[_python_type(f), Field(description=help_text) if help_text else Field()]
        if f.required and not all_optional:
            fields[f.name] = (typ, ...)
        else:
            fields[f.name] = (typ | None, None)
    return pydantic.create_model(name, **fields)


def _make_tools(spec: RecordsSpec) -> list[Tool]:
    item = spec.item
    entry_field = (Annotated[str, Field(description=f"which {item}: its #number or its {spec.title_field}")], ...)

    has_clock = any(f.type == "datetime" for f in spec.fields)
    ambiguity = (
        {"time_ambiguous": (Annotated[bool, Field(description="true if the hour could mean two times (e.g. '2')")], False)}
        if has_clock
        else {}
    )
    remind = (
        {
            "remind_before_minutes": (
                Annotated[
                    int | None,
                    Field(
                        ge=0,
                        le=60 * 24 * 14,
                        description="only if the user asks to be reminded about this entry: minutes before (60 if they didn't say)",
                    ),
                ],
                None,
            )
        }
        if spec.time_fields
        else {}
    )
    add_model = _args_model(f"{spec.name}_add", spec, all_optional=False, extra={**ambiguity, **remind})
    update_model = _args_model(
        f"{spec.name}_update", spec, all_optional=True, extra={"entry": entry_field, **ambiguity, **remind}
    )
    # A list: "hepsini sil" is one call, not one call per entry (live, the model
    # sometimes deleted them one per step and ran out of steps).
    delete_model = pydantic.create_model(
        f"{spec.name}_delete",
        entries=(
            Annotated[list[str], Field(min_length=1, description=f"which {item} entries: #numbers or {spec.title_field}s")],
            ...,
        ),
    )
    scope = (Literal["upcoming", "all"], "upcoming") if spec.time_fields else (Literal["all"], "all")
    list_model = pydantic.create_model(
        f"{spec.name}_list",
        scope=scope,
        query=(Annotated[str | None, Field(description="optional words to search for")], None),
    )

    def add_entry(
        ctx: ToolContext, time_ambiguous: bool = False, remind_before_minutes: int | None = None, **values: Any
    ) -> ToolResult:
        data, error = _parse_values(spec, values, ctx)
        if error:
            return ToolResult(error, final=False)  # the model fixes the value or asks the user
        if (unsaid := _unsaid_time(spec, data, ctx)) is not None:
            # "8 kasım" with no clock time arrived as 00:00 (live, 2026-11-05), and in a
            # replay as an invented 09:00. Don't save a time nobody said.
            field, at = unsaid
            return ToolResult(
                f"The user never said the clock time {at} for '{field}'. If the request is about an existing "
                "entry, use update_entry with its #number. Otherwise ask the user what time, and call no tool.",
                final=False,
            )
        if time_ambiguous and (question := _ambiguity_question(spec, data, ctx)):
            return ToolResult(question)  # nothing saved; the answer comes as the next message
        if remind_before_minutes is not None:
            data[REMIND_KEY] = remind_before_minutes
        now = utc_now_iso()
        cur = ctx.db.execute(
            "INSERT INTO records (user_id, agent, data, sort_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.user_id, spec.name, json.dumps(data, ensure_ascii=False), _sort_at(spec, data, ctx), now, now),
        )
        # The #number in the reply lets the next message ("1 saat önce hatırlat")
        # point at this entry directly, without listing first.
        text = ctx.t("added", emoji=_emoji(spec), id=cur.lastrowid, summary=_summary(spec, data, ctx))
        reply = _with_reminders(spec, cur.lastrowid, data, ctx, text)
        if spec.time_fields and not spec.reminders and remind_before_minutes is None and _in_future(spec, data, ctx):
            # No automatic reminders and none asked for: save first, then offer one
            # (the user asked for this). "1 saat önce hatırlat" next -> update_entry.
            reply += " " + ctx.t("ask_reminder")
        return ToolResult(reply)

    def list_entries(ctx: ToolContext, scope: str = "upcoming", query: str | None = None) -> str:
        rows = _rows(spec, ctx)
        if scope == "upcoming" and spec.time_fields:
            start = ctx.today_start_utc()
            rows = [r for r in rows if r["sort_at"] is None or r["sort_at"] >= start]
        if query:
            rows = [r for r in rows if find_unique(query, [(_summary(spec, _data(r), ctx), r)]) is not None]
        if not rows:
            return ctx.t("list_empty", title=spec.title)
        lines = [ctx.t("list_header", title=spec.title)]
        lines += [f"#{r['id']} · {_summary(spec, _data(r), ctx)}" for r in rows]
        return "\n".join(lines)

    def update_entry(
        ctx: ToolContext,
        entry: str,
        time_ambiguous: bool = False,
        remind_before_minutes: int | None = None,
        **values: Any,
    ) -> ToolResult:
        row = _find(spec, entry, ctx)
        if row is None:
            return ToolResult(ctx.t("not_found", entry=entry))
        given = _keep_unchanged_part(spec, {k: v for k, v in values.items() if v is not None}, _data(row), ctx)
        changes, error = _parse_values(spec, given, ctx, partial=True)
        if error:
            return ToolResult(error, final=False)
        if time_ambiguous and (question := _ambiguity_question(spec, changes, ctx)):
            return ToolResult(question)
        if remind_before_minutes is not None:
            changes[REMIND_KEY] = remind_before_minutes
        data = {**_data(row), **changes}
        ctx.db.execute(
            "UPDATE records SET data = ?, sort_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), _sort_at(spec, data, ctx), utc_now_iso(), row["id"]),
        )
        reminders.cancel_for_record(ctx.db, spec.name, row["id"])
        text = ctx.t("updated", summary=_summary(spec, data, ctx))
        return ToolResult(_with_reminders(spec, row["id"], data, ctx, text))

    def delete_entry(ctx: ToolContext, entries: list[str]) -> str:
        lines = []
        for entry in dict.fromkeys(entries):  # "#2" twice is one deletion
            row = _find(spec, entry, ctx)
            if row is None:
                lines.append(ctx.t("not_found", entry=entry))
                continue
            ctx.db.execute("UPDATE records SET deleted_at = ? WHERE id = ?", (utc_now_iso(), row["id"]))
            reminders.cancel_for_record(ctx.db, spec.name, row["id"])
            lines.append(ctx.t("deleted", summary=_summary(spec, _data(row), ctx)))
        return "\n".join(lines)

    definitions = [
        ("add_entry", f"Save a new {item}.", add_entry, add_model),
        ("list_entries", f"Show {item} entries to the user. Not needed before update_entry or delete_entry.", list_entries, list_model),
        ("update_entry", f"Change fields of an existing {item}; send only the fields that change.", update_entry, update_model),
        ("delete_entry", f"Delete one or more {item} entries in one call (all of them: every #number).", delete_entry, delete_model),
    ]
    return [
        Tool(name, desc, fn, model, ToolSpec(name, desc, clean_schema(model.model_json_schema())))
        for name, desc, fn, model in definitions
    ]


# --- helpers -----------------------------------------------------------------------


def _parse_values(spec: RecordsSpec, values: dict[str, Any], ctx: ToolContext, partial: bool = False):
    """Validate and normalize field values. Returns (data, error_for_the_model)."""
    data: dict[str, Any] = {}
    today = ctx.now.date()
    for f in spec.fields:
        value = values.get(f.name)
        if value is None or value == "":
            if f.required and not partial:
                return {}, f"Missing required field '{f.name}'. Ask the user for it."
            continue
        if f.type == "date":
            day = resolve_day(str(value), today)
            if day is None:
                return {}, f"Field '{f.name}': '{value}' is not a valid date. Use {DATE_HELP}."
            data[f.name] = day.isoformat()
        elif f.type == "datetime":
            moment = parse_datetime(str(value), today)
            if moment is None:
                return {}, f"Field '{f.name}': '{value}' is not a valid date-time. Use {DATETIME_HELP}."
            data[f.name] = moment.isoformat(timespec="minutes")
        else:
            data[f.name] = value
    return data, None


# Ways to actually say midnight; anything else that became 00:00 was a date without a time.
_MIDNIGHT = re.compile(r"00[:.]00|24[:.]00|gece\s*yar[ıi]s|gece\s*12|midnight|12\s*am|saat\s*00?\b", re.IGNORECASE)
_NOON = re.compile(r"öğle|ogle|noon", re.IGNORECASE)


# Ways people write a clock hour: "14:00", "9.30", "saat 9", "2'de", "akşam 8", "9 buçuk", "3pm".
# A bare number is not enough: "8 kasım" is a date, "#2" an entry, "1 saat önce" a duration.
_HOUR_PATTERNS = [
    re.compile(r"(?<![\d#])(\d{1,2})[:.]\d{2}(?!\d)"),
    re.compile(r"\b(?:saat|at)\s*(\d{1,2})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![\d#])(\d{1,2})\s*['’]?\s*(?:de|da|te|ta|e|a|ye|ya)\b", re.IGNORECASE),
    re.compile(r"\b(?:sabah|öğle(?:den sonra)?|ogleden sonra|akşam|aksam|gece)\s*(\d{1,2})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![\d#])(\d{1,2})\s*(?:buçuk|bucuk|gibi|am\b|pm\b)", re.IGNORECASE),
]


def said_hours(text: str) -> set[int]:
    """Clock hours mentioned in a text (as written: "akşam 8" gives 8)."""
    hours = set()
    for pattern in _HOUR_PATTERNS:
        hours |= {int(h) for h in pattern.findall(text) if int(h) <= 24}
    return hours


def _unsaid_time(spec: RecordsSpec, data: dict[str, Any], ctx: ToolContext) -> tuple[str, str] | None:
    """(field, "HH:MM") for a clock time nobody said: not in the request, not in the chat.

    Lenient on purpose: the hour only has to be mentioned ("akşam 8" counts for
    20:00; the bot's own "14:00 mi, 02:00 mı?" counts for 14:00). It catches a time
    made up from nothing (live: 00:00, in a replay: 09:00), not every mistake.
    """
    if not ctx.request_text:
        return None  # not called from a chat message (tests, jobs): nothing to check against
    said = f"{ctx.request_text}\n{ctx.conversation}"
    hours = said_hours(said)
    for f in spec.fields:
        if f.type != "datetime" or not data.get(f.name):
            continue
        moment = datetime.fromisoformat(data[f.name])
        if moment.hour == moment.minute == 0:
            ok = _MIDNIGHT.search(said) is not None or 24 in hours
        else:
            ok = bool({moment.hour, moment.hour % 12 or 12} & hours) or (moment.hour == 12 and _NOON.search(said))
        if not ok:
            return f.name, f"{moment:%H:%M}"
    return None


def _keep_unchanged_part(spec: RecordsSpec, values: dict[str, Any], old: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """'Move it to the 9th' changes the day, not the time; '16:00 olsun' changes the time, not the day.

    For a date-time field the model may send only a date or only a time; the other
    half is taken from the stored value. (Live: asked to move a 15:00 meeting to
    another day, the model sent '2026-11-09 00:00'. Code keeps the time now.)
    """
    today = ctx.now.date()
    out = dict(values)
    for f in spec.fields:
        value, previous = out.get(f.name), old.get(f.name)
        if f.type != "datetime" or value is None or not previous:
            continue
        before = datetime.fromisoformat(previous)
        text = str(value).strip()
        if (moment := parse_datetime(text, today)) is not None:
            if moment.hour == moment.minute == 0 and ctx.request_text and not _MIDNIGHT.search(ctx.request_text):
                out[f.name] = f"{moment.date().isoformat()} {before:%H:%M}"  # a date sent as "... 00:00"
            continue  # a full date-time: use as is
        if (day := resolve_day(text, today)) is not None:
            out[f.name] = f"{day.isoformat()} {before:%H:%M}"
        elif (at := parse_time(text)) is not None:
            out[f.name] = f"{before.date().isoformat()} {at:%H:%M}"
    return out


def _ambiguity_question(spec: RecordsSpec, data: dict[str, Any], ctx: ToolContext) -> str | None:
    """'2' -> 'öğleden sonra 14:00 mı, yoksa 02:00 mı?' Written by code, not the model (ADR-0011)."""
    for f in spec.fields:
        if f.type == "datetime" and data.get(f.name):
            moment = datetime.fromisoformat(data[f.name])
            hour = moment.hour % 12 or 12
            am, pm = moment.replace(hour=hour % 12), moment.replace(hour=hour % 12 + 12)
            said = f"{hour}.{moment.minute:02d}" if moment.minute else str(hour)  # "1.30", "2"
            return ctx.t("ambiguous_time", hour=said, am=f"{am:%H:%M}", pm=f"{pm:%H:%M}")
    return None


def _local(spec_field: FieldSpec, value: str, ctx: ToolContext) -> datetime:
    """A stored date/datetime value as an aware datetime in the user's zone (dates at 09:00)."""
    if spec_field.type == "date":
        return datetime.combine(date.fromisoformat(value), datetime.min.time().replace(hour=9), tzinfo=ctx.now.tzinfo)
    return datetime.fromisoformat(value).replace(tzinfo=ctx.now.tzinfo)


def _sort_at(spec: RecordsSpec, data: dict[str, Any], ctx: ToolContext) -> str | None:
    for f in spec.time_fields:
        if data.get(f.name):
            return _local(f, data[f.name], ctx).astimezone(UTC).isoformat(timespec="seconds")
    return None


def _in_future(spec: RecordsSpec, data: dict[str, Any], ctx: ToolContext) -> bool:
    fields = [f for f in spec.time_fields if data.get(f.name)]
    return any(_local(f, data[f.name], ctx) > ctx.now for f in fields)


def _reminder_rules(spec: RecordsSpec, data: dict[str, Any]) -> list[ReminderSpec]:
    """The ability's default rules, or the reminder the user asked for on this entry."""
    asked = data.get(REMIND_KEY)
    if asked is None:
        return spec.reminders
    # Prefer a clock time over a whole day for a reminder the user asked for.
    times = sorted(spec.time_fields, key=lambda f: f.type != "datetime")
    target = next((f for f in times if data.get(f.name)), None)
    return [ReminderSpec(field=target.name, before_minutes=asked)] if target else []


def _with_reminders(spec: RecordsSpec, record_id: int, data: dict[str, Any], ctx: ToolContext, text: str) -> str:
    fields = {f.name: f for f in spec.fields}
    weekdays = ctx.t("weekdays").split(",")
    notes = []
    for r in _reminder_rules(spec, data):
        if not data.get(r.field):
            continue
        due = _local(fields[r.field], data[r.field], ctx) - timedelta(minutes=r.before_minutes)
        if due <= ctx.now:
            continue  # already past: don't schedule a reminder for the past
        reminders.schedule(
            ctx.db, ctx.user_id, spec.name, due, ctx.t("reminder", summary=_summary(spec, data, ctx)), record_id
        )
        notes.append(ctx.t("remind_at", at=format_datetime(due.replace(tzinfo=None), weekdays)))
    return " ".join(p for p in [text, *notes] if p)


def resync_reminders(spec: RecordsSpec, ctx: ToolContext) -> int:
    """Re-create reminders for every entry of an agent (after its reminder rules changed).

    Pending reminders are cancelled and rebuilt; past moments get none. Returns how
    many reminders are pending afterwards.
    """
    for row in _rows(spec, ctx):
        reminders.cancel_for_record(ctx.db, spec.name, row["id"])
        _with_reminders(spec, row["id"], _data(row), ctx, "")
    row = ctx.db.one(
        "SELECT COUNT(*) AS n FROM reminders WHERE agent = ? AND user_id = ? AND status = 'pending'",
        (spec.name, ctx.user_id),
    )
    return row["n"]


def format_minutes(minutes: int, units: tuple[str, str, str]) -> str:
    """60 -> '1 saat', 1440 -> '1 gün', 45 -> '45 dk' (units: minute, hour, day)."""
    minute, hour, day = units
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440} {day}"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60} {hour}"
    return f"{minutes} {minute}"


def _rows(spec: RecordsSpec, ctx: ToolContext) -> list:
    return ctx.db.query(
        "SELECT * FROM records WHERE user_id = ? AND agent = ? AND deleted_at IS NULL "
        "ORDER BY sort_at IS NULL, sort_at, id",
        (ctx.user_id, spec.name),
    )


def _data(row) -> dict[str, Any]:
    return json.loads(row["data"])


def _find(spec: RecordsSpec, entry: str, ctx: ToolContext):
    rows = _rows(spec, ctx)
    key = entry.strip().lstrip("#")
    if key.isdigit():
        return next((r for r in rows if r["id"] == int(key)), None)
    return find_unique(entry, [(str(_data(r).get(spec.title_field, "")), r) for r in rows])


def _summary(spec: RecordsSpec, data: dict[str, Any], ctx: ToolContext) -> str:
    weekdays = ctx.t("weekdays").split(",")
    parts = []
    for f in spec.fields:
        value = data.get(f.name)
        if value in (None, ""):
            continue
        if f.type == "datetime":
            parts.append(format_datetime(datetime.fromisoformat(value), weekdays))
        elif f.type == "date":
            parts.append(format_day(date.fromisoformat(value), weekdays))
        elif f.type == "number":
            parts.append(f"{value:g}" if isinstance(value, float) else str(value))
        else:
            parts.append(str(value).strip().rstrip(".!"))  # templates add their own punctuation
    return " · ".join(parts)


def _emoji(spec: RecordsSpec) -> str:
    first = spec.title.split(" ", 1)[0]
    return first if first and not first[0].isalnum() else "✅"
