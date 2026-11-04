"""Fixed texts the core sends (errors, pairing, help). Agents keep their own in messages.yaml."""

from __future__ import annotations

from typing import Any

SUPPORTED_LANGUAGES = ("tr", "en")

CORE_MESSAGES: dict[str, dict[str, str]] = {
    "tr": {
        "error_llm": "Şu an yapay zekâ servisine ulaşamıyorum ({kind}). Biraz sonra tekrar dener misin?",
        "error_auth": "Yapay zekâ servisi API anahtarını kabul etmedi. Botu kuran kişi `make doctor` çalıştırmalı.",
        "error_internal": "Bir şeyler ters gitti, kaydettim. Tekrar dener misin?",
        "too_many_steps": "Bunu tamamlayamadım. Biraz daha basit yazar mısın?",
        "empty_reply": "Tamam.",
        "not_paired": "Bu bot henüz eşleştirilmedi. Botu kuran kişi, terminalde görünen 6 haneli kodu buraya göndermeli.",
        "paired": "✅ Eşleşme tamam! Artık bana yazabilirsin.\n\nÖrnek: \"zeytinyağı almam lazım\"",
        "help": "Bana normal cümlelerle yaz, gerisini ben hallederim.\n\nŞunları yapabiliyorum:\n{agents}\n\n"
        "Komutlar:\n/agents · yeteneklerim\n/status · aktif model ve bugünkü kullanım\n/trace · son mesajını nasıl işlediğim",
        "status": "🤖 {provider} / {model}\n📅 Bugün: {messages} mesaj, {calls} LLM çağrısı, {tokens} token, ${cost}",
        "no_trace": "Henüz işlenmiş bir mesajın yok.",
        "late_prefix": "(gecikmeli)",
        "agents_header": "🧩 Yeteneklerim:",
        "agent_builtin": "hazır",
        "agent_by_bot": "senin için eklendi",
    },
    "en": {
        "error_llm": "I can't reach the AI service right now ({kind}). Could you try again in a bit?",
        "error_auth": "The AI service rejected the API key. Whoever set up the bot should run `make doctor`.",
        "error_internal": "Something went wrong; I've logged it. Could you try again?",
        "too_many_steps": "I couldn't finish that. Could you put it more simply?",
        "empty_reply": "Okay.",
        "not_paired": "This bot isn't paired yet. Whoever set it up should send the 6-digit code shown in their terminal.",
        "paired": "✅ Paired! You can talk to me now.\n\nTry: \"I need olive oil\"",
        "help": "Just write to me in plain sentences.\n\nThis is what I can do:\n{agents}\n\n"
        "Commands:\n/agents · what I can do\n/status · active model and today's usage\n/trace · how I handled your last message",
        "status": "🤖 {provider} / {model}\n📅 Today: {messages} messages, {calls} LLM calls, {tokens} tokens, ${cost}",
        "no_trace": "You don't have a handled message yet.",
        "late_prefix": "(late)",
        "agents_header": "🧩 What I can do:",
        "agent_builtin": "built in",
        "agent_by_bot": "added for you",
    },
}


def t(language: str, key: str, **values: Any) -> str:
    table = CORE_MESSAGES.get(language, CORE_MESSAGES["en"])
    return table.get(key, CORE_MESSAGES["en"][key]).format(**values)
