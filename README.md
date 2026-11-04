# erranderpy

**Small personal agents you talk to on Telegram.**

> "I need olive oil" → added to your shopping list.
> "From now on I'll study English one hour a day" → becomes a daily habit.
> "Studied English for 30 minutes today" → today's task is 30/60 min done.

- **A tiny agent platform.** Each agent (shopping, tasks, …) is a folder. Adding a new agent
  means adding a folder; the core never changes.
- **Bring any LLM key.** Works with Anthropic, Gemini, DeepSeek or OpenAI — whichever API key you
  put in `.env` is used.
- **It watches itself.** Every message is traced, and an on-call agent investigates errors and
  wrong decisions, then reports back to you on Telegram.
- **Two commands to run.** `git clone` and `./start.sh`. No Docker, no database server.

> 🚧 **Status: early development (Stage 0).** The quick start below describes the target.
> Follow progress in [SPEC.md](SPEC.md).

## Quick start (target)

```bash
git clone https://github.com/firatkill/erranderpy.git && cd erranderpy
./start.sh
```

The setup wizard asks for a Telegram bot token and one LLM API key, then prints a pairing code.
Send that code to your bot and start chatting.

## Development

```bash
make test       # offline tests, no API calls
make test-live  # contract tests against every provider with a key in .env (< $0.01)
make eval       # Turkish eval set with the provider the bot would use
make compare    # same eval set on every provider with a key → reports/eval-*.md
```

## Eval results

46 Turkish cases (30 routing, 16 tool calls), each run 3 times
([report](reports/2026-11-04-openai-baseline.md)):

| Model | Routing | Tool calls | Errors | p50 latency | Cost / call |
|---|---|---|---|---|---|
| OpenAI gpt-6-luna | 100% | 100% | 0% | 1.65 s | $0.000053 |

Other providers will be added as they are measured. A perfect score also means the eval set is
too easy to tell models apart, so harder cases are next.

## Roadmap

| Stage | What | Status |
|---|---|---|
| 0 | Provider-agnostic LLM layer + Turkish eval set + model comparison | ✅ |
| 1 | Core (router, agent runtime, tracing), shopping agent, `start.sh` setup | ⏳ |
| 2 | Tasks & habits agent, nightly scheduler, evening summary | ⏳ |
| 3 | On-call agent and chaos tests | ⏳ |
| 4 | `make new-agent`, Docker, public release | ⏳ |

## License

[MIT](LICENSE)
