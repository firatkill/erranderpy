# AGENTS.md

Guide for anyone (human or coding agent) changing this codebase: how it is put together, the rules
it follows, and why. The README explains how to *use* the bot; this file explains how to *work on* it.

## What this is

A personal Telegram assistant made of small agents (shopping, tasks and habits, notes, and
abilities the bot creates for itself), behind a provider-neutral LLM layer, with tracing, evals
and an on-call agent that watches the bot. Python 3.12, uv, SQLite, python-telegram-bot (polling).

## Map

```
erranderpy/
  __main__.py          start: settings → LLM → DB → agents → on-call → Telegram
  assistant.py         the pipeline without Telegram: history → router → agent(s) → reply, traced
  gateway/telegram.py  the only Telegram code: pairing guard, commands, scheduled jobs, reminders
  core/
    llm/               LLMProvider interface; openai_compat (OpenAI, DeepSeek, Gemini) + anthropic_native
    router.py          one LLM call with a `route` tool → [Route(agent, text, requests)]
    runtime.py         the agent loop: tool calls, early exit, multi-request check, MAX_STEPS
    registry.py        loads agent folders (code agents and `kind: records`)
    records.py         declarative "records" agents: tools, prompt and replies built from YAML
    reminders.py       persistent one-off reminders (30 s tick)
    tools.py           @tool, ToolContext, ToolResult
    jobs.py            @daily jobs
    dates.py, text.py  date parsing (in code, never by the LLM); Turkish-aware matching
    db.py, tracing.py  SQLite; one trace per message, one span per step
  oncall/              signals (code) → diagnosis (LLM, read-only tools) → alerts / report; canary; chaos
  evals/               eval runner and scoring
  new_agent.py         make new-agent
agents/<name>/         one folder per agent (see agents/README.md); _template/ is the scaffold
evals/                 router_tr.yaml, tools_tr.yaml, fixture agents (evals/agents/)
deploy/service.sh      make service: launchd (macOS) / systemd (Linux)
tests/                 offline tests; tests/live/ calls real APIs (make test-live)
data/                  runtime only, git-ignored: database, abilities the bot created, logs
SPEC.md                the source of truth for features and stages (Turkish)
```

## Commands

```bash
make test        # offline, free; run before every commit
make eval        # router + tool-call eval with the configured provider (a cent or two)
make test-live   # contract tests against every provider with a key
make compare     # eval on every provider with a key
make chaos       # inject 5 faults, check on-call alerts and diagnoses (a few cents)
make doctor      # setup check (`--offline` skips network calls; CI uses it)
make new-agent NAME=x
```

Never read or print `.env`; you may only check whether a key is set.

## Principles

1. **The LLM decides, code does.** The model only produces structured output: which agent, which
   tool, which arguments. Dates, storage, matching, counting and reply text are deterministic code.
2. **Guard in code, don't plead in the prompt.** When the model gets something wrong, prefer a
   check in code (a classification flag, a validation, a refusal with `final=False` so the model
   retries or asks) over another sentence in the prompt. Every live bug so far was fixed this way:
   the multi-request check, the unsaid-time guard, `time_ambiguous`, same-draft guards.
3. **Replies come from templates.** Tools write the user-facing text from `messages.yaml` (tr, en).
   A tool result is final by default and the loop exits early: no extra LLM call to rephrase.
4. **Provider independence.** Agents and the router never import an LLM SDK. No `temperature`,
   no forced `tool_choice`; consistency comes from clear prompts, structured output and evals.
5. **Easy setup is sacred.** A clean clone must run with `./start.sh`. CI checks it on every push.
6. **Adding an agent never touches the core.** If it seems to, the plug-in contract is missing
   something: fix the contract, not the agent.
7. **Never guess between two candidates.** Name matching (`text.find_unique`, `phrases_match`) is
   Turkish-aware (suffixes, consonant softening) and returns nothing when two things match.

## How a message flows

1. `gateway/telegram.py` checks the sender is the paired owner, then calls `Assistant.handle`.
2. The router (one LLM call) splits the message into requests per agent. Several requests for one
   agent become one route carrying the user's exact words plus the list of requests.
3. `runtime.run_agent` runs the agent loop. Records agents also see their current entries in the
   prompt. With 2+ requests, a one-time check asks the model whether any request was missed.
4. Tools run with validated arguments; their template replies are joined and sent.
5. Every step is a span in `traces` (`/trace` shows the last one). The user's message and the reply
   go to `messages` (the last 6 are context for the next message).

## Conventions

- **Agents:** see `agents/README.md`. `description` and `examples` are what the router sees; say where
  the agent's border is with the others. Never copy eval sentences into `examples`.
- **Tools:** first parameter `ctx: ToolContext`; type hints become the JSON schema (Pydantic),
  the docstring is the description. Return `ToolResult(text, final=False)` when the model must see
  the result and continue.
- **SQL:** always `?` placeholders. Agent tables are prefixed with the agent name, created by
  `schema.sql` with `IF NOT EXISTS`. Agents never write each other's tables.
- **Time:** timestamps are stored in UTC; "today" is the user's local day (`TIMEZONE`).
  Scheduled jobs must be idempotent.
- **Language:** code, comments, commit messages and README in English. The bot answers in the
  user's language. `SPEC.md` is Turkish. Module docstrings explain the module's role; comments
  explain *why*, not *what*.
- **Evals:** every message the bot got wrong in real use becomes a case (router_tr.yaml or the
  agent's evals.yaml). Expectation forms and matchers are documented in `evals/tools_tr.yaml`.
  A case must describe the system as it is now: update cases that predate a design change.
- **Tests:** offline with `tests/fakes.py` (`ScriptedLLM`, `route_answer`, `tool_answer`). For a model
  behaviour bug, also replay the real message with the real model on a copy of the database.
- **Commits:** Conventional Commits, tests pass first, no AI attribution lines.

## Debugging a live conversation

1. Find the trace: `/trace` on Telegram, or `SELECT * FROM traces WHERE trace_id = ? ORDER BY id`
   in `data/assistant.db` (the router span shows the routes; `agent.llm` spans show tool calls).
2. Decide where it went wrong: routing, the agent's choice, a tool, or missing context.
3. Write an eval case (or a test) that fails, fix it (in code where possible), replay the real
   message a few times with the real model, run `make test` and `make eval`.

## Decisions

The reasoning behind each decision, in one or two lines. Code comments refer to these numbers.

- **ADR-0001 Telegram, long polling.** No open port, domain or HTTPS; works behind NAT and next to nginx.
- **ADR-0002 Four providers, two adapters.** Anthropic → Gemini → DeepSeek → OpenAI, chosen by which
  key is set (`LLM_PROVIDER`, `LLM_MODEL` override). `openai_compat` covers OpenAI, DeepSeek, Gemini;
  `anthropic_native` covers Anthropic. Nothing else imports an SDK.
- **ADR-0003 The LLM decides, code does.** Structured decisions only; all work in code (principle 1).
- **ADR-0004 Setup: `start.sh` + uv; Docker optional.** uv installs Python; a wizard writes `.env`
  and checks the keys; the owner pairs with a 6-digit code.
- **ADR-0005 SQLite.** One file, `data/assistant.db`, plain SQL through the standard library.
- **ADR-0006 No forced tool calls.** Only `tool_choice` auto/none; instruct, then validate
  (`no_tool_call` is an error the router turns into `unknown`).
- **ADR-0007 Fast decision mode, no temperature.** Per-provider low-reasoning settings in `defaults.py`.
- **ADR-0008 Code writes the reply; early exit.** Template replies, final tool results end the loop.
- **ADR-0009 Agent plug-in contract.** An agent is a folder; tools are typed Python functions.
- **ADR-0010 Scheduled jobs belong to agents.** `@daily` in `jobs.py`, no LLM, idempotent, time from env.
- **ADR-0011 Missing info: save first, code asks.** E.g. a habit without minutes is saved and the
  template reply asks for them; ambiguous clock times are asked by code (`time_ambiguous`).
- **ADR-0012 Declarative agents (`kind: records`).** A validated YAML spec; the platform builds tools,
  prompt and replies. Shared `records` table.
- **ADR-0013 The builder agent: approval, no code.** Proposes a spec, creates it only after the user
  says yes, in `data/agents/` (git-ignored); then replays the request that started it.
- **ADR-0014 Persistent reminders.** A `reminders` table and a 30 s tick; late ones within 6 h are
  sent marked late, older ones are dropped.
- **ADR-0015 Undo instead of "are you sure?".** Clearing the shopping list happens at once and can be undone.
- **ADR-0016 Several requests for one agent.** The router's split travels with the route; the agent
  is told the list; a one-time check runs for every multi-request message; questions after it are
  kept; repeated identical calls are skipped.
- **ADR-0017 Records agents see their entries.** Upcoming entries with #numbers are in the prompt on
  every turn; a clock time that no time expression in the chat mentions is not saved.
- **ADR-0018 On-call: code detects, the LLM explains, code writes.** Signals from traces without an
  LLM; critical ones alert at once (6 h cooldown), written by code so they arrive when the LLM is
  down; a daily report and `/oncall` add a diagnosis from read-only tools. Not routable; its messages
  stay out of the chat history; secrets are redacted.
- **ADR-0019 Canary.** Before each report the router eval runs against the running agents: silent
  misroutes (no error anywhere) become an `eval_regression` signal.
- **ADR-0020 New agent = copy of a working template.** `make new-agent` copies `agents/_template`
  and a test; the copy loads and passes before any change.
- **ADR-0021 Background service.** `make service`: launchd / systemd, restart on failure only; a
  start flag cleared on clean stop reveals crashes, and the bot says it restarted.

New decisions get the next number and a line here.
