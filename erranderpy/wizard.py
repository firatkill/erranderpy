"""First-run wizard: ask for a Telegram token and one LLM key, check both, write .env.

Run by ./start.sh when .env doesn't exist. Every value is verified with a real
(free or sub-cent) call before it is saved, so a typo shows up now, not later.
"""

from __future__ import annotations

import getpass
import os
import sys

from erranderpy.config import ROOT
from erranderpy.core.llm import build_provider
from erranderpy.core.llm.defaults import PROVIDERS
from erranderpy.doctor import check_llm, check_telegram

ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"
ATTEMPTS = 3

KEY_PAGES = {
    "anthropic": "https://platform.claude.com/settings/keys",
    "gemini": "https://aistudio.google.com/apikey",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "openai": "https://platform.openai.com/api-keys",
}


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def ask_secret(prompt: str) -> str:
    return getpass.getpass(f"{prompt} (hidden while you paste): ").strip()


def main() -> int:
    if ENV_FILE.exists():
        print(".env already exists; nothing to do. (Delete it to run the wizard again.)")
        return 0
    if not sys.stdin.isatty():
        print("No .env found and this is not an interactive terminal.")
        print("Copy .env.example to .env, fill in TELEGRAM_BOT_TOKEN and one LLM key, then start again.")
        return 1

    print("\nWelcome to erranderpy! Two things are needed: a Telegram bot token and one LLM API key.\n")
    values: dict[str, str] = {}

    language = ask("Bot language (tr/en)", "tr").lower()
    values["BOT_LANGUAGE"] = language if language in ("tr", "en") else "tr"

    print("\n1) Telegram bot token")
    print("   • Open Telegram and talk to @BotFather")
    print("   • Send /newbot, pick a name and a username ending in 'bot'")
    print("   • BotFather replies with a token like 123456:ABC-DEF...")
    for _ in range(ATTEMPTS):
        token = ask_secret("   Paste the token")
        ok, message = check_telegram(token)
        print(("   ✓ " if ok else "   ✗ ") + message)
        if ok:
            values["TELEGRAM_BOT_TOKEN"] = token
            break
    else:
        print("\nCouldn't verify the token. Nothing was saved; run ./start.sh to try again.")
        return 1

    print("\n2) LLM provider. Which API key do you have?")
    for i, spec in enumerate(PROVIDERS, start=1):
        print(f"   {i}) {spec.name:<10} default model: {spec.default_model}")
    choice = ask("   Number", "1")
    spec = PROVIDERS[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(PROVIDERS) else PROVIDERS[0]
    print(f"   Get a key here: {KEY_PAGES[spec.name]}")
    for _ in range(ATTEMPTS):
        key = ask_secret(f"   Paste your {spec.name} API key")
        ok, message = check_llm(build_provider(spec.name, {spec.env_key: key}))
        print(("   ✓ " if ok else "   ✗ ") + message)
        if ok:
            values[spec.env_key] = key
            break
    else:
        print("\nCouldn't verify the API key. Nothing was saved; run ./start.sh to try again.")
        return 1

    values["TIMEZONE"] = ask("\n3) Your time zone", "Europe/Istanbul")

    write_env(values)
    print("\n✓ Saved to .env (only readable by you). Starting the bot...\n")
    return 0


def write_env(values: dict[str, str]) -> None:
    """Fill the template's empty `KEY=` lines; keep its comments for the reader."""
    lines = []
    seen = set()
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            lines.append(line)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    # Create the file with owner-only permissions *before* writing secrets into it.
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was saved.")
        sys.exit(1)
