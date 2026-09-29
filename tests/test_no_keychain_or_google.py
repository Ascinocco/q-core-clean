"""The root conftest's guard: no test reads the Keychain or calls Google."""
import pytest

from api import google_calendar


def test_the_keychain_is_unreachable_from_tests():
    with pytest.raises(AssertionError, match="Keychain"):
        google_calendar._keychain(google_calendar.KEYCHAIN_SERVICE, "refresh-token")


def test_google_is_unreachable_from_tests():
    with pytest.raises(AssertionError, match="Google"):
        google_calendar._form("https://oauth2.googleapis.com/token", {"invented": "value"})


def test_other_commands_still_run():
    assert google_calendar.subprocess.run(["true"]).returncode == 0
