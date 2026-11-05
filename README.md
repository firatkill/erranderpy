# erranderpy

**Small personal agents you talk to on Telegram.**

> "I need olive oil" → added to your shopping list.
> "From now on I'll study English one hour a day" → becomes a daily habit.
> "Studied English for 30 minutes today" → today's task is 30/60 min done.

- **A tiny agent platform.** Each agent (shopping, tasks, notes, …) is a folder. `make new-agent`
  gives you a working one to start from; the core never changes.
- **Bring any LLM key.** Works with Anthropic, Gemini, DeepSeek or OpenAI — whichever API key you
  put in `.env` is used.
- **It grows itself.** Ask for something it can't do yet ("11 kasımda 14:00'te AVM") and it proposes a
  new ability (a calendar), adds it after you say yes, and handles your request. No code is generated:
  it writes a validated YAML description and the platform builds the agent.
- **It watches itself.** Every message is traced, and an on-call agent investigates errors and
  wrong decisions, then reports back to you on Telegram.
- **Two commands to run.** `git clone` and `./start.sh`. No Docker, no database server, no open
  ports. `make service` keeps it running in the background.

> **Status: v1 complete (Stage 5).** Shopping, tasks and habits, notes, abilities the bot adds to
> itself (with reminders), and an on-call agent that watches the bot. See [SPEC.md](SPEC.md).

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

## Keep it running

```bash
make service          # macOS: launchd agent · Linux: systemd service (asks for sudo)
make service-status
make logs
make service-uninstall
```

It starts at login (Mac) or boot (Linux) and restarts after a crash. If a run ends uncleanly
(crash, kill, power cut), the bot tells you when it's back. On a Mac the bot sleeps when the Mac
sleeps; messages wait on Telegram and are answered when it wakes.

**Docker instead** (if it's already on your server):

```bash
cp .env.example .env    # fill in TELEGRAM_BOT_TOKEN and one LLM key
docker compose up -d
docker compose logs -f  # the pairing code is here on the first start
```

Telegram long polling means the bot opens no port: no domain, no HTTPS, nothing to add to nginx.

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

```bash
make new-agent NAME=recipes
```

This copies [agents/_template](agents/_template/) into `agents/recipes/`: a small agent that already
works (add, list, delete) with its table, reply templates, eval file and a test in `tests/`.
Describe it in `agent.yaml`, shape `tools.py`, add real messages to `evals.yaml`, then
`make test && make eval` and restart. The router picks it up; nothing in the core changes.
The [notes agent](agents/notes/) was made exactly this way. More in [agents/README.md](agents/README.md).

## Development

```bash
make doctor     # check .env, Telegram token, LLM key, agents, database
make test       # offline tests, no API calls
make test-live  # contract tests against every provider with a key in .env (< $0.01)
make eval       # Turkish eval set with the provider the bot would use
make compare    # same eval set on every provider with a key → reports/eval-*.md
make chaos      # inject 5 faults, check on-call alerts and diagnoses (a few cents)
make new-agent NAME=x
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) checks the setup promise on every push:
a clean clone installs with uv, passes the tests and `make doctor --offline`, a new agent from the
template works untouched, the systemd unit is valid, and the Docker image builds.

## Eval results

98 Turkish cases (45 routing, 53 tool calls across all agents), each run 3 times
([report](reports/2026-11-05-openai.md)):

| Model | Routing | Tool calls | Errors | p50 latency | Cost / call |
|---|---|---|---|---|---|
| OpenAI gpt-6-luna | 100% | 99% | 0% | 1.52 s | $0.000100 |

The one miss was an outdated case (fixed after the run, see the report). Many cases come from real
conversations: each message the bot got wrong became a case. Other providers will be added as they
are measured (`make compare`).

On-call, `make chaos` ([report](reports/2026-11-05-chaos.md)): of 5 injected faults, 5 alerted
correctly and 5 were diagnosed correctly, in 2 runs.

## Roadmap

| Stage | What | Status |
|---|---|---|
| 0 | Provider-agnostic LLM layer + Turkish eval set + model comparison | ✅ |
| 1 | Core (router, agent runtime, tracing), shopping agent, `start.sh` setup | ✅ |
| 2 | Tasks & habits agent, nightly scheduler, evening summary | ✅ |
| 3 | Self-extending bot: builder agent, declarative agents, reminders | ✅ |
| 4 | On-call agent, online eval (canary), chaos tests | ✅ |
| 5 | `make new-agent` + notes agent, `make service`, Docker, CI | ✅ |

## License

[MIT](LICENSE)
