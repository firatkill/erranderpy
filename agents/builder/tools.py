"""The agent builder: lets the bot add new abilities to itself, from chat.

Safety by design (docs/decisions/0013-builder-onayli-ve-kodsuz.md):
- It never writes code. It writes one YAML file, a RecordsSpec, validated by
  Pydantic before anything is saved; the platform builds the tools from it.
- Nothing is created without the user's "yes": propose first, create after.
- It writes only under the user's agent folder (data/agents/), only new names
  (a strict pattern, no paths), and can only remove agents it created itself.
  Removing moves the folder aside instead of deleting it.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated

import pydantic
import yaml
from pydantic import BaseModel, Field

from erranderpy.core.records import (
    FieldSpec,
    RecordsSpec,
    ReminderSpec,
    build_records_agent,
    format_minutes,
    resync_reminders,
)
from erranderpy.core.registry import AgentLoadError, load_agent
from erranderpy.core.tools import ToolContext, ToolResult, tool

DEFAULT_REMINDER_MINUTES = 60

TYPE_LABELS = {
    "tr": {"text": "metin", "number": "sayı", "date": "tarih", "datetime": "tarih-saat", "choice": "seçenek"},
    "en": {"text": "text", "number": "number", "date": "date", "datetime": "date-time", "choice": "choice"},
}


class AgentDraft(BaseModel):
    """A proposed ability. Same fields as RecordsSpec, minus what the platform sets."""

    name: str = Field(description="English snake_case, e.g. 'calendar'")
    title: str = Field(description="one emoji + short name in the user's language, e.g. '📅 Takvim'")
    item: str = Field(description="what one entry is called, in the user's language, e.g. 'etkinlik'")
    description: str = Field(description="English: what it stores and when to use it (the router reads this)")
    examples: list[str] = Field(default_factory=list, description="3-4 short example messages in the user's language")
    fields: list[FieldSpec]
    reminders: list[ReminderSpec] = Field(default_factory=list)


@tool
def propose_agent(
    ctx: ToolContext,
    draft: AgentDraft,
    handle_after_creation: Annotated[
        bool,
        Field(description="true if the user's message also contained a concrete entry to save once the ability exists"),
    ] = False,
    without_reminders: Annotated[
        bool, Field(description="true only if the user explicitly doesn't want reminders")
    ] = False,
) -> ToolResult:
    """Propose a new ability, or a change to one you created (same name), for the user to approve. Nothing is saved yet."""
    if ctx.extras.get("user_agents_dir") is None:
        return ToolResult(ctx.t("disabled"))
    values = draft.model_dump()
    # A clock time without a reminder is rarely what anyone wants. Default in code,
    # not by asking the model nicely: in a live test it left reminders out.
    if not values["reminders"] and not without_reminders:
        clock = next((f for f in draft.fields if f.type == "datetime"), None)
        if clock is not None:
            values["reminders"] = [{"field": clock.name, "before_minutes": DEFAULT_REMINDER_MINUTES}]
    try:
        spec = RecordsSpec(**values, created_by="builder")
    except pydantic.ValidationError as exc:
        return ToolResult(f"The draft is invalid, fix it and propose again: {exc.errors(include_url=False)}", final=False)

    agents = ctx.extras.get("agents", {})
    existing = agents.get(spec.name)
    is_update = existing is not None and existing.created_by == "builder"
    if not is_update and (existing is not None or (_user_dir(ctx) / spec.name).exists()):
        what = f"({existing.title}: {existing.description})" if existing else ""
        return ToolResult(
            f"An agent named '{spec.name}' already exists {what}, and it is not one you created. If it already "
            "does what the user wants, tell them to just ask; otherwise propose a different name.",
            final=False,
        )

    previous = _pending(ctx)
    if previous is not None and _same(previous["spec"], spec):
        # The model re-proposed what the user is already looking at (seen live
        # after the user answered "Ekle"). Don't show it again; let it decide.
        return ToolResult(
            "This is identical to the proposal the user is already looking at. If the user agreed "
            "(e.g. 'evet', 'ekle', 'olur', 'tamam'), call create_agent now; otherwise ask what to change.",
            final=False,
        )

    # The request to handle after creation is kept in the user's own words
    # (ctx.request_text), never rewritten by the model: in a live test the model
    # "helpfully" turned "saat 2" into "14:00" and the ambiguity was never asked.
    request = ctx.request_text if handle_after_creation else (previous or {}).get("request")
    pending = {"spec": spec.model_dump(mode="json"), "request": request, "update": is_update}
    ctx.db.set_setting(_key(ctx), json.dumps(pending, ensure_ascii=False))
    return ToolResult(_preview(ctx, spec, is_update))


@tool
def create_agent(ctx: ToolContext) -> ToolResult:
    """Create (or apply the change to) the pending proposal after the user said yes."""
    # No arguments on purpose: the user approves exactly what they were shown,
    # and the model can't create anything that wasn't proposed first.
    if ctx.extras.get("user_agents_dir") is None:
        return ToolResult(ctx.t("disabled"))
    pending = _pending(ctx)
    if pending is None:
        return ToolResult("There is no pending proposal. Propose an ability first.", final=False)
    spec = RecordsSpec.model_validate(pending["spec"])
    is_update = bool(pending.get("update"))

    folder = _user_dir(ctx) / spec.name
    if folder.exists() and not is_update:
        return ToolResult(f"'{spec.name}' already exists; propose a different name.", final=False)
    backup = None
    if is_update and folder.exists():
        # Keep the old version aside; entries stay in the database either way.
        backup = _user_dir(ctx) / "_deleted" / f"{spec.name}-{datetime.now():%Y%m%d%H%M%S}-before-update"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, backup)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "agent.yaml").write_text(
        yaml.safe_dump(spec.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    try:
        load_agent(folder)  # prove it loads before telling anyone it exists
    except AgentLoadError as exc:
        shutil.rmtree(folder)
        if backup is not None:
            shutil.copytree(backup, folder)  # put the working version back
        return ToolResult(f"Saving failed, nothing changed: {exc}", final=False)

    ctx.db.execute("DELETE FROM settings WHERE key = ?", (_key(ctx),))
    ctx.effects["reload_agents"] = True
    if is_update:
        # New reminder rules apply to entries that already exist, too.
        agent_ctx = replace(ctx, messages=build_records_agent(spec).messages_for(ctx.t("lang")))
        count = resync_reminders(spec, agent_ctx)
        return ToolResult(ctx.t("updated", title=spec.title, reminders=count))
    if pending.get("request"):
        ctx.effects["replay"] = pending["request"]
    return ToolResult(ctx.t("created", title=spec.title, example=(spec.examples or [spec.item])[0]))


@tool
def cancel_proposal(ctx: ToolContext) -> str:
    """Drop the pending proposal (the user said no)."""
    ctx.db.execute("DELETE FROM settings WHERE key = ?", (_key(ctx),))
    return ctx.t("cancelled")


@tool
def list_abilities(ctx: ToolContext) -> str:
    """List what the bot can do, marking abilities created on request."""
    lines = [ctx.t("list_header")]
    for agent in ctx.extras.get("agents", {}).values():
        mark = ctx.t("by_bot") if agent.created_by == "builder" else ctx.t("builtin")
        lines.append(f"• {agent.title or agent.name} · {mark}")
    return "\n".join(lines)


@tool
def delete_agent(ctx: ToolContext, name: str) -> str:
    """Remove an ability that was created on request. Its entries are kept."""
    agent = ctx.extras.get("agents", {}).get(name)
    if ctx.extras.get("user_agents_dir") is None or agent is None or agent.created_by != "builder" or agent.path is None:
        return ctx.t("not_deletable", name=name)
    # Move aside instead of deleting: folders starting with "_" are not loaded,
    # and the entries stay in the database, so re-creating it brings them back.
    archive = _user_dir(ctx) / "_deleted" / f"{name}-{datetime.now():%Y%m%d%H%M%S}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(agent.path), archive)
    ctx.effects["reload_agents"] = True
    return ctx.t("deleted", title=agent.title or name)


# --- helpers -------------------------------------------------------------------


def _user_dir(ctx: ToolContext) -> Path:
    return Path(ctx.extras["user_agents_dir"])


def _key(ctx: ToolContext) -> str:
    return f"builder.pending.{ctx.user_id}"


def _pending(ctx: ToolContext) -> dict | None:
    value = ctx.db.get_setting(_key(ctx))
    return json.loads(value) if value else None


def _same(previous: dict, spec: RecordsSpec) -> bool:
    """Same ability as already proposed? Wording (description, examples) doesn't count."""
    keys = ("name", "fields", "reminders")
    current = spec.model_dump(mode="json")
    return all(previous.get(k) == current.get(k) for k in keys)


def _preview(ctx: ToolContext, spec: RecordsSpec, is_update: bool = False) -> str:
    language = "tr" if ctx.t("lang") == "tr" else "en"
    types = TYPE_LABELS[language]
    parts = []
    for f in spec.fields:
        detail = types[f.type] + (f" ({', '.join(f.options or [])})" if f.type == "choice" else "")
        if f.required:
            detail += ", " + ctx.t("required")
        parts.append(f"{f.label or f.name} ({detail})")
    labels = {f.name: f.label or f.name for f in spec.fields}
    units = (ctx.t("unit_minute"), ctx.t("unit_hour"), ctx.t("unit_day"))
    lines = [
        ctx.t("update_proposal" if is_update else "proposal", title=spec.title),
        ctx.t("fields", item=spec.item, fields=" · ".join(parts)),
    ]
    lines += [
        ctx.t("reminder", field=labels[r.field], before=format_minutes(r.before_minutes, units)) for r in spec.reminders
    ]
    lines += ["", ctx.t("confirm")]
    return "\n".join(lines)
