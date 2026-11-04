import pytest

from erranderpy.config import load_settings
from erranderpy.core.db import Database
from erranderpy.core.llm import ConfigError
from erranderpy.pairing import MAX_ATTEMPTS, Pairing


def test_first_correct_code_wins_and_code_is_consumed():
    db = Database(":memory:")
    pairing = Pairing(db)
    assert not pairing.paired and len(pairing.code) == 6
    assert not pairing.try_pair(1, 1, "000000" if pairing.code != "000000" else "111111")
    assert pairing.try_pair(2, 2, f"  {pairing.code} ")
    assert pairing.is_owner(2) and not pairing.is_owner(1)
    assert pairing.code is None
    assert Pairing(db).paired  # survives a restart


def test_wrong_codes_get_blocked():
    pairing = Pairing(Database(":memory:"))
    code = pairing.code
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MAX_ATTEMPTS):
        pairing.try_pair(9, 9, wrong)
    assert pairing.blocked(9)
    assert not pairing.try_pair(9, 9, code)  # even the right code is refused now


def test_settings_defaults_and_errors():
    s = load_settings({"TELEGRAM_BOT_TOKEN": "123:abc"})
    assert s.language == "tr" and s.timezone.key == "Europe/Istanbul"
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_settings({})
    with pytest.raises(ConfigError, match="TIMEZONE"):
        load_settings({"TELEGRAM_BOT_TOKEN": "x", "TIMEZONE": "Mars/Olympus"})
    with pytest.raises(ConfigError, match="BOT_LANGUAGE"):
        load_settings({"TELEGRAM_BOT_TOKEN": "x", "BOT_LANGUAGE": "de"})
