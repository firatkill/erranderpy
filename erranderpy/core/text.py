"""Small text helpers shared by agents and evals."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

MIN_STEM = 4  # shorter words must match exactly ("su" must not match "sucuk")


def tr_lower(text: str) -> str:
    """Lowercase Turkish correctly.

    Python's str.lower() turns "I" into "i" and "İ" into "i̇" (i + combining
    dot), both wrong for Turkish. We map the dotted/dotless pairs first.
    """
    return text.replace("I", "ı").replace("İ", "i").lower()


def normalize(text: str) -> str:
    """Lowercase (Turkish-aware) and collapse whitespace: the key we compare names by."""
    return " ".join(tr_lower(text).split())


# Turkish softens a final p/ç/t/k before a vowel suffix: kitap → kitabı, ekmek → ekmeği.
SOFTENED = {"p": "b", "ç": "c", "t": "d", "k": "ğ"}


def _words_match(a: str, b: str) -> bool:
    """'ingilizceyi' ~ 'ingilizce', 'ekmeği' ~ 'ekmek': one word is the other plus suffixes."""
    short, long = sorted((a, b), key=len)
    if len(short) < MIN_STEM:
        return False
    if long.startswith(short):
        return True
    stem, last = short[:-1], short[-1]
    return last in SOFTENED and long.startswith(stem + SOFTENED[last])


def phrases_match(query: str, candidate: str) -> bool:
    """Every word of the shorter phrase matches some word of the longer one."""
    q, c = normalize(query).split(), normalize(candidate).split()
    if not q or not c:
        return False
    short, long = sorted((q, c), key=len)
    return all(any(_words_match(s, w) for w in long) for s in short)


MIN_SUBSTRING = 3  # "yağ" may find "zeytinyağı"; "su" must not find "sucuk"


def _contains(a: str, b: str) -> bool:
    short, long = sorted((a, b), key=len)
    return len(short) >= MIN_SUBSTRING and short in long


def find_unique(query: str, candidates: Sequence[tuple[str, T]]) -> T | None:
    """Pick the candidate whose name matches `query`, but only if there is exactly one.

    `candidates` is a list of (name, value). An exact (normalized) name wins;
    otherwise a single loose match ("zeytin" -> "zeytinyağı", "ingilizceyi" ->
    "ingilizce okuma"). Two loose matches means we don't guess: the caller
    tells the user it couldn't find it.
    """
    key = normalize(query)
    if not key:
        return None
    exact = [value for name, value in candidates if normalize(name) == key]
    if len(exact) == 1:
        return exact[0]
    loose = [value for name, value in candidates if _contains(key, normalize(name)) or phrases_match(key, name)]
    return loose[0] if len(loose) == 1 else None
