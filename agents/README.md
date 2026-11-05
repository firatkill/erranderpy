# Agents

Each folder here is one agent. The core finds them on start; adding an agent never requires
editing core code.

```bash
make new-agent NAME=recipes     # copies _template/ into recipes/ and a test into tests/
```

The template is a small working agent (add / list / delete): it loads, passes its test and answers
on Telegram before you change anything. Then fill the TODOs. `make doctor` warns while the
description still says TODO. The `notes/` agent was created this way.

There are two kinds:
- **code agents** (this folder): you write `tools.py`. Full power.
- **records agents**: a YAML description only (`kind: records`); the platform generates the tools.
  This is what the bot writes when you ask it for a new ability (the `builder` agent). Those land in
  `data/agents/` (private, git-ignored), never here. See `evals/agents/calendar/agent.yaml` for an example.

```
agents/<name>/
  agent.yaml     name (= folder name), description, examples, system_prompt   required
  tools.py       functions marked with @tool                                 required
  jobs.py        functions marked with @daily (run at a time of day)         optional
  schema.sql     CREATE TABLE IF NOT EXISTS ... for this agent's data        optional
  messages.yaml  reply templates per language (tr, en)                       optional
  evals.yaml     eval cases for this agent's tool calls                      optional
```

## How a message reaches an agent

1. The **router** sees every agent's `description` and `examples` and picks one (or several).
2. The **agent loop** gives the model this agent's `system_prompt` and tools.
3. The model calls a tool; **your function runs** with validated arguments.
4. If the tool returns a plain string (a *final* result), that string is the reply. No extra LLM call.

## Writing a tool

```python
from erranderpy.core.tools import ToolContext, tool

@tool
def add_items(ctx: ToolContext, items: list[Item]) -> str:
    """Add one or more items to the shopping list."""   # the model reads this
    ...
    return ctx.t("added", items="...")                  # template from messages.yaml
```

- The first parameter is always `ctx` (database, user id, current time, templates).
- Type hints become the JSON schema the model sees, and arguments are validated against them.
- Use `ctx.db` with `?` placeholders, never f-strings in SQL.
- Do the work in code (dates, counting, matching); the model only decides.
- Return `ToolResult(text, final=False)` if the model should see the result and continue.

## Scheduled jobs

```python
from erranderpy.core.jobs import daily

@daily(at="21:00", env="SUMMARY_TIME")       # env lets users move it or turn it "off"
def evening_summary(ctx: ToolContext) -> str | None:
    ...                                       # returned text is sent to the owner; None sends nothing
```

`jobs.py` can import helpers from the same agent's tools: `from erranderpy_agents.<name>.tools import ...`.
Make jobs idempotent: they may run twice, or not at all if the machine was asleep.

## Checking a new agent

```bash
make test     # loads every agent; a broken folder fails with a clear message
make eval     # includes the cases in your evals.yaml
```

Never copy sentences from `evals.yaml` into `examples`: the eval would measure recall, not understanding.

See `notes/` for an agent built from the template, `shopping/` for a complete example, and the decision record (ADR-0009 in [AGENTS.md](../AGENTS.md))
for why it works this way.
