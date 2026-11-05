"""The on-call agent: watches the bot's own traces, alerts on problems, explains them.

    traces + reminders ──▶ signals.py (code: what looks wrong)
                               │
                               ▼
                         agent.py (LLM: why, with read-only tools) ──▶ report (code formats it)
                               │
                               ▼
                         service.py (when: every 5 min for alerts, 08:00 report, /oncall)

It is not in agents/: the router never sends user messages to it, and its
messages never enter the chat history. See ADR-0018 in AGENTS.md.
"""
