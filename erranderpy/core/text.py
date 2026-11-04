"""Small text helpers shared by agents and evals."""

from __future__ import annotations


def tr_lower(text: str) -> str:
    """Lowercase Turkish correctly.

    Python's str.lower() turns "I" into "i" and "İ" into "i̇" (i + combining
    dot), both wrong for Turkish. We map the dotted/dotless pairs first.
    """
    return text.replace("I", "ı").replace("İ", "i").lower()


def normalize(text: str) -> str:
    """Lowercase (Turkish-aware) and collapse whitespace: the key we compare names by."""
    return " ".join(tr_lower(text).split())
