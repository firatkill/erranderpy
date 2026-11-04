"""Declarative agents: an agent described in YAML, with no code of its own.

A "records" agent keeps a list of entries with typed fields and can remind the
user before a date/time field. From the spec the platform builds the four
tools (add/list/update/delete), their JSON schemas, the system prompt and the
replies. This is what lets the bot create new abilities for itself safely:
the builder agent only writes a validated spec; every line of code that runs
is code we wrote and tested. See docs/decisions/0012-bildirimsel-agentlar.md.

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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import pydantic
from pydantic import BaseModel, Field, model_validator

from erranderpy.core import reminders
from erranderpy.core.agent import Agent
from erranderpy.core.dates import format_datetime, format_day, parse_datetime, resolve_day
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
        "added": "{emoji} Kaydettim: {summary}.",
        "updated": "✏️ Güncelledim: {summary}.",
        "deleted": "🗑️ Sildim: {summary}.",
        "not_found": "\"{entry}\" diye bir kayıt bulamadım.",
        "remind_at": "⏰ {at} hatırlatacağım.",
        "list_header": "{title}",
        "list_empty": "{title}: kayıt yok.",
        "reminder": "⏰ Hatırlatma: {summary}",
        "ambiguous_time": "🕑 Saat {hour} derken öğleden sonra {pm} mi, yoksa {am} mi?",
        "weekdays": "Pazartesi,Salı,Çarşamba,Perşembe,Cuma,Cumartesi,Pazar",
    },
    "en": {
        "added": "{emoji} Saved: {summary}.",
        "updated": "✏️ Updated: {summary}.",
        "deleted": "🗑️ Deleted: {summary}.",
        "not_found": "I couldn't find an entry \"{entry}\".",
        "remind_at": "⏰ I'll remind you at {at}.",
        "list_header": "{title}",
        "list_empty": "{title}: no entries.",
        "reminder": "⏰ Reminder: {summary}",
        "ambiguous_time": "🕑 By {hour}, do you mean {pm} in the afternoon, or {am}?",
        "weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday",
    },
}

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
    )


def _system_prompt(spec: RecordsSpec) -> str:
    lines = [f"You keep the user's {spec.title} — a list of {spec.item} entries. Fields:"]
    for f in spec.fields:
        extra = f" (one of: {', '.join(f.options or [])})" if f.type == "choice" else ""
        need = "required" if f.required else "optional"
        lines.append(f"- {f.name}: {f.type}, {need}{extra}. {f.description}".rstrip())
    if any(f.type == "datetime" for f in spec.fields):
        lines.append(
            "Clock times: if the user's hour could mean two times (\"saat 2\" = 02:00 or 14:00) and they gave no "
            "hint like sabah/öğleden sonra/akşam/gece, still call the tool with your best guess and set "
            "time_ambiguous=true: the tool then asks the user instead of saving. \"akşam 8\" is 20:00, not ambiguous."
        )
    lines += [
        "Tools: add_entry saves a new entry, list_entries shows them, update_entry changes one, delete_entry removes one.",
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
    add_model = _args_model(f"{spec.name}_add", spec, all_optional=False, extra=ambiguity)
    update_model = _args_model(f"{spec.name}_update", spec, all_optional=True, extra={"entry": entry_field, **ambiguity})
    delete_model = pydantic.create_model(f"{spec.name}_delete", entry=entry_field)
    scope = (Literal["upcoming", "all"], "upcoming") if spec.time_fields else (Literal["all"], "all")
    list_model = pydantic.create_model(
        f"{spec.name}_list",
        scope=scope,
        query=(Annotated[str | None, Field(description="optional words to search for")], None),
    )

    def add_entry(ctx: ToolContext, time_ambiguous: bool = False, **values: Any) -> ToolResult:
        data, error = _parse_values(spec, values, ctx)
        if error:
            return ToolResult(error, final=False)  # the model fixes the value or asks the user
        if time_ambiguous and (question := _ambiguity_question(spec, data, ctx)):
            return ToolResult(question)  # nothing saved; the answer comes as the next message
        now = utc_now_iso()
        cur = ctx.db.execute(
            "INSERT INTO records (user_id, agent, data, sort_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.user_id, spec.name, json.dumps(data, ensure_ascii=False), _sort_at(spec, data, ctx), now, now),
        )
        text = ctx.t("added", emoji=_emoji(spec), summary=_summary(spec, data, ctx))
        return ToolResult(_with_reminders(spec, cur.lastrowid, data, ctx, text))

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

    def update_entry(ctx: ToolContext, entry: str, time_ambiguous: bool = False, **values: Any) -> ToolResult:
        row = _find(spec, entry, ctx)
        if row is None:
            return ToolResult(ctx.t("not_found", entry=entry))
        changes, error = _parse_values(spec, {k: v for k, v in values.items() if v is not None}, ctx, partial=True)
        if error:
            return ToolResult(error, final=False)
        if time_ambiguous and (question := _ambiguity_question(spec, changes, ctx)):
            return ToolResult(question)
        data = {**_data(row), **changes}
        ctx.db.execute(
            "UPDATE records SET data = ?, sort_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), _sort_at(spec, data, ctx), utc_now_iso(), row["id"]),
        )
        reminders.cancel_for_record(ctx.db, spec.name, row["id"])
        text = ctx.t("updated", summary=_summary(spec, data, ctx))
        return ToolResult(_with_reminders(spec, row["id"], data, ctx, text))

    def delete_entry(ctx: ToolContext, entry: str) -> str:
        row = _find(spec, entry, ctx)
        if row is None:
            return ctx.t("not_found", entry=entry)
        ctx.db.execute("UPDATE records SET deleted_at = ? WHERE id = ?", (utc_now_iso(), row["id"]))
        reminders.cancel_for_record(ctx.db, spec.name, row["id"])
        return ctx.t("deleted", summary=_summary(spec, _data(row), ctx))

    definitions = [
        ("add_entry", f"Save a new {item}.", add_entry, add_model),
        ("list_entries", f"Show {item} entries.", list_entries, list_model),
        ("update_entry", f"Change fields of an existing {item}; send only the fields that change.", update_entry, update_model),
        ("delete_entry", f"Delete a {item}.", delete_entry, delete_model),
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


def _ambiguity_question(spec: RecordsSpec, data: dict[str, Any], ctx: ToolContext) -> str | None:
    """'2' -> 'öğleden sonra 14:00 mı, yoksa 02:00 mı?' Written by code, not the model (ADR-0011)."""
    for f in spec.fields:
        if f.type == "datetime" and data.get(f.name):
            moment = datetime.fromisoformat(data[f.name])
            hour = moment.hour % 12 or 12
            am, pm = moment.replace(hour=hour % 12), moment.replace(hour=hour % 12 + 12)
            return ctx.t("ambiguous_time", hour=hour, am=f"{am:%H:%M}", pm=f"{pm:%H:%M}")
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


def _with_reminders(spec: RecordsSpec, record_id: int, data: dict[str, Any], ctx: ToolContext, text: str) -> str:
    fields = {f.name: f for f in spec.fields}
    weekdays = ctx.t("weekdays").split(",")
    notes = []
    for r in spec.reminders:
        if not data.get(r.field):
            continue
        due = _local(fields[r.field], data[r.field], ctx) - timedelta(minutes=r.before_minutes)
        if due <= ctx.now:
            continue  # already past: don't schedule a reminder for the past
        reminders.schedule(
            ctx.db, ctx.user_id, spec.name, due, ctx.t("reminder", summary=_summary(spec, data, ctx)), record_id
        )
        notes.append(ctx.t("remind_at", at=format_datetime(due.replace(tzinfo=None), weekdays)))
    return " ".join([text, *notes])


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
            parts.append(str(value))
    return " · ".join(parts)


def _emoji(spec: RecordsSpec) -> str:
    first = spec.title.split(" ", 1)[0]
    return first if first and not first[0].isalnum() else "✅"
