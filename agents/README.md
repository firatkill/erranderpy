# Agents

Each folder here is one agent. The core finds them on start; adding an agent never requires
editing core code.

```
agents/<name>/
  agent.yaml     name (= folder name), description, examples, system_prompt   required
  tools.py       functions marked with @tool                                 required
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

## Checking a new agent

```bash
make test     # loads every agent; a broken folder fails with a clear message
make eval     # includes the cases in your evals.yaml
```

Never copy sentences from `evals.yaml` into `examples`: the eval would measure recall, not understanding.

See `shopping/` for a complete example, and the [decision record](../docs/decisions/0009-agent-eklenti-sozlesmesi.md)
for why it works this way.
