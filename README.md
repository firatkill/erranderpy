# erranderpy

**Small personal agents you talk to on Telegram.**

> "I need olive oil" → added to your shopping list.
> "From now on I'll study English one hour a day" → becomes a daily habit.
> "Studied English for 30 minutes today" → today's task is 30/60 min done.

- **A tiny agent platform.** Each agent (shopping, tasks, …) is a folder. Adding a new agent
  means adding a folder; the core never changes.
- **Bring any LLM key.** Works with Anthropic, Gemini, DeepSeek or OpenAI — whichever API key you
  put in `.env` is used.
- **It grows itself.** Ask for something it can't do yet ("11 kasımda 14:00'te AVM") and it proposes a
  new ability (a calendar), adds it after you say yes, and handles your request. No code is generated:
  it writes a validated YAML description and the platform builds the agent.
- **It watches itself.** Every message is traced, and an on-call agent investigates errors and
  wrong decisions, then reports back to you on Telegram.
- **Two commands to run.** `git clone` and `./start.sh`. No Docker, no database server.

> 🚧 **Status: Stage 4.** Shopping, tasks and habits, abilities the bot adds to itself (with reminders),
> and an on-call agent that watches the bot. Packaging and release come next.
> Follow progress in [SPEC.md](SPEC.md).

## Quick start

```bash
git clone https://github.com/firatkill/erranderpy.git && cd erranderpy
./start.sh
```

The first run installs everything ([uv](https://docs.astral.sh/uv/) downloads the right Python), then a
setup wizard asks for a Telegram bot token and one LLM API key and checks both. The bot prints a
6-digit pairing code: send it to your bot, and from then on it answers only you.

Then just write to it:

```
you: 2 paket makarna, bir de süt
bot: 🛒 Ekledim: 2 × makarna, süt. Listede 2 ürün var.
you: her gün 30 dk ingilizce okuyacağım, bir de diksiyon çalışmalıyım
bot: 🔁 Yeni alışkanlık: ingilizce okuma, günde 30 dk. Bugünden başlıyoruz.
     🔁 Yeni alışkanlık: diksiyon. Günde kaç dakika ayırmak istersin? …
you: /trace
bot: 🔎 router.decide · 1.9s → shopping … tool.call add_items · 0.00s … 💰 $0.0001
```

Type `/` for the command menu: `/help`, `/agents`, `/status` (active model, today's usage), `/trace`, `/oncall`.
`make doctor` checks the whole setup.

## On-call

The bot watches itself. Code checks the traces every 5 minutes and alerts at once on critical
problems (rejected API key, LLM outage, a crashing tool, reminders not going out). The alert is
written by code, so it still arrives when the LLM is the thing that's broken. Every morning at 08:00
(or on `/oncall`) an on-call agent reads the signals, opens traces and agent configs with read-only
tools, and explains what went wrong and what to change:

```
🩺 Oncall raporu · son 24 saat
📊 31 mesaj · 66 LLM çağrısı · $0.0070 · p95 6.8 sn · 0 başarısız mesaj

🟠 Alışveriş mesajları tasks agent'ına gidiyor
   Neden: Çalışan shopping açıklaması hava durumuyla ilgili; router alışverişi tanımıyor.
   Öneri: agents/shopping/agent.yaml açıklamasını geri al ve botu yeniden başlat.
```

`make chaos` breaks the bot on purpose in five ways and checks that on-call notices and explains each.

## Adding an agent

Create `agents/<name>/` with an `agent.yaml` and a `tools.py`. That's it: the router picks it up
on the next start. See [agents/README.md](agents/README.md) and the [shopping agent](agents/shopping/).

## Development

```bash
make doctor     # check .env, Telegram token, LLM key, agents, database
make test       # offline tests, no API calls
make test-live  # contract tests against every provider with a key in .env (< $0.01)
make eval       # Turkish eval set with the provider the bot would use
make compare    # same eval set on every provider with a key → reports/eval-*.md
make chaos      # inject 5 faults, check on-call alerts and diagnoses (a few cents)
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
| 1 | Core (router, agent runtime, tracing), shopping agent, `start.sh` setup | ✅ |
| 2 | Tasks & habits agent, nightly scheduler, evening summary | ✅ |
| 3 | Self-extending bot: builder agent, declarative agents, reminders | ✅ |
| 4 | On-call agent, online eval (canary), chaos tests | ✅ |
| 5 | `make new-agent`, Docker, public release | ⏳ |

## License

[MIT](LICENSE)
