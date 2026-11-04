"""Decide who owns the bot, without asking anyone to look up their Telegram ID.

On first start the bot prints a 6-digit code in the terminal. Whoever sends
that code to the bot becomes the owner; the bot ignores everyone else.
Only someone who can see the terminal can pair. See ADR-0004.
"""

from __future__ import annotations

import secrets

from erranderpy.core.db import Database

OWNER_USER_KEY = "owner_user_id"
OWNER_CHAT_KEY = "owner_chat_id"
MAX_ATTEMPTS = 5  # wrong codes per Telegram user before we stop answering them


class Pairing:
    def __init__(self, db: Database):
        self.db = db
        self.code: str | None = None if self.owner_id is not None else f"{secrets.randbelow(10**6):06d}"
        self._failed: dict[int, int] = {}

    @property
    def owner_id(self) -> int | None:
        value = self.db.get_setting(OWNER_USER_KEY)
        return int(value) if value is not None else None

    @property
    def paired(self) -> bool:
        return self.owner_id is not None

    def is_owner(self, user_id: int) -> bool:
        return self.owner_id == user_id

    def blocked(self, user_id: int) -> bool:
        return self._failed.get(user_id, 0) >= MAX_ATTEMPTS

    def try_pair(self, user_id: int, chat_id: int, text: str) -> bool:
        """True if `text` is the code: `user_id` becomes the owner."""
        if self.paired or self.code is None or self.blocked(user_id):
            return False
        # compare_digest takes the same time whether the first digit or the last
        # one is wrong, so the code can't be guessed digit by digit from timing.
        if secrets.compare_digest(text.strip(), self.code):
            self.db.set_setting(OWNER_USER_KEY, user_id)
            self.db.set_setting(OWNER_CHAT_KEY, chat_id)
            self.code = None
            return True
        self._failed[user_id] = self._failed.get(user_id, 0) + 1
        return False
