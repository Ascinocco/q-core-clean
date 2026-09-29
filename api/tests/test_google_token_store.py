"""Google OAuth on Linux: the file token store and the Serve redirect (ticket T-43).

Invented values only. The Keychain path is macOS-only and unchanged.
"""
import stat

import pytest

from api import google_calendar as google
from api.config import get_settings


@pytest.fixture
def file_store(test_settings, monkeypatch):
    settings = test_settings.model_copy(update={"google_token_store": "file",
                                                "google_oauth_client_id": "invented-client-id",
                                                "google_oauth_client_secret": "invented-client-secret"})
    monkeypatch.setattr(google, "get_settings", lambda: settings)
    return settings


def test_the_file_store_writes_a_private_file_atomically(file_store):
    google._store_refresh_token("invented-refresh-1")
    path = google._token_file()
    assert path.read_text() == "invented-refresh-1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    google._store_refresh_token("invented-refresh-2")  # replaced, never appended
    assert google._refresh_token() == "invented-refresh-2"
    assert [p.name for p in path.parent.iterdir()] == ["google-refresh-token"]  # no temp left


def test_a_missing_token_is_a_clear_not_connected_error(file_store):
    with pytest.raises(google.CredentialsUnavailable, match="not connected"):
        google._refresh_token()


def test_refresh_uses_the_file_token_and_settings_credentials(file_store, monkeypatch):
    google._store_refresh_token("invented-refresh")
    sent = {}
    monkeypatch.setattr(google, "_form", lambda url, values: sent.update(values) or {"access_token": "invented-access"})
    assert google._access_token() == "invented-access"
    assert sent == {"client_id": "invented-client-id", "client_secret": "invented-client-secret",
                    "refresh_token": "invented-refresh", "grant_type": "refresh_token"}


def test_the_file_store_never_falls_back_to_the_keychain(test_settings, monkeypatch):
    settings = test_settings.model_copy(update={"google_token_store": "file"})
    monkeypatch.setattr(google, "get_settings", lambda: settings)
    monkeypatch.setattr(google.subprocess, "run", lambda *a, **k: pytest.fail("keychain used on the file store"))
    with pytest.raises(google.CredentialsUnavailable):
        google._client_credentials()


def test_a_missing_keychain_tool_is_credentials_unavailable(test_settings, monkeypatch):
    """`security` is absent on Linux; that must not surface as a 500."""
    settings = test_settings.model_copy(update={"google_token_store": "keychain"})
    monkeypatch.setattr(google, "get_settings", lambda: settings)

    def missing(*args, **kwargs):
        raise FileNotFoundError("security")

    monkeypatch.setattr(google.subprocess, "run", missing)
    with pytest.raises(google.CredentialsUnavailable):
        google._client_credentials()


def test_the_default_store_follows_the_platform(test_settings, monkeypatch):
    unset = test_settings.model_copy(update={"google_token_store": None})
    monkeypatch.setattr(google, "get_settings", lambda: unset)
    monkeypatch.setattr(google.sys, "platform", "linux")
    assert google._token_store() == "file"
    monkeypatch.setattr(google.sys, "platform", "darwin")
    assert google._token_store() == "keychain"


def test_the_redirect_goes_through_serve_when_a_serve_hostname_is_set(test_settings, monkeypatch):
    monkeypatch.setattr(google, "get_settings", lambda: test_settings)
    assert google._redirect_uri() == f"http://localhost:{test_settings.port}/integrations/google/callback"
    served = test_settings.model_copy(update={"serve_hostname": "q-core.tail1234.ts.net"})
    monkeypatch.setattr(google, "get_settings", lambda: served)
    assert google._redirect_uri() == "https://q-core.tail1234.ts.net/integrations/google/callback"


def test_connect_without_credentials_is_503_not_500(client, test_settings, monkeypatch):
    settings = test_settings.model_copy(update={"google_token_store": "file"})
    monkeypatch.setattr(google, "get_settings", lambda: settings)
    response = client.post("/integrations/google/connect", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 503


def test_callback_stores_the_token_in_the_file_store(client, file_store, monkeypatch):
    google._states.add("invented-state")
    monkeypatch.setattr(google, "_form", lambda url, values: {"refresh_token": "invented-refresh", "access_token": "a"})
    monkeypatch.setattr(google, "_request", lambda *a, **k: {"id": "invented-calendar"})
    response = client.get("/integrations/google/callback", params={"code": "invented-code", "state": "invented-state"})
    assert response.status_code == 200 and response.json()["connected"] is True
    assert google._refresh_token() == "invented-refresh"
    assert "invented-refresh" not in response.text


def test_a_failed_replace_leaves_no_temp_file_and_keeps_the_old_token(file_store, monkeypatch):
    google._store_refresh_token("old-refresh")
    path = google._token_file()

    def refuse(*args):
        raise OSError("invented failure")

    monkeypatch.setattr(google.os, "replace", refuse)
    with pytest.raises(OSError):
        google._store_refresh_token("new-refresh")
    assert path.read_text() == "old-refresh"
    assert [p.name for p in path.parent.iterdir()] == ["google-refresh-token"]


def test_the_directory_is_fsynced_after_the_rename(file_store, monkeypatch):
    """The rename survives a power cut only once the directory entry is on disk."""
    synced, real_fsync = [], google.os.fsync

    def recording(fd):
        synced.append(google.os.path.isdir(f"/dev/fd/{fd}") or stat.S_ISDIR(google.os.fstat(fd).st_mode))
        return real_fsync(fd)

    monkeypatch.setattr(google.os, "fsync", recording)
    google._store_refresh_token("abc")
    assert synced == [False, True]  # the file, then its directory


def test_both_stores_word_a_missing_token_the_same(test_settings, monkeypatch):
    monkeypatch.setattr(google, "get_settings", lambda: test_settings.model_copy(update={"google_token_store": "keychain"}))

    def missing(*args, **kwargs):
        raise google.subprocess.CalledProcessError(44, "security")

    monkeypatch.setattr(google.subprocess, "run", missing)
    with pytest.raises(google.CredentialsUnavailable, match="not connected"):
        google._refresh_token()


def test_a_callback_after_credentials_vanished_is_503_not_500(client, file_store, monkeypatch):
    google._states.add("state-1")
    # The file store, so nothing falls back to this machine's Keychain; and no
    # network: reaching Google here would be the bug.
    gone = file_store.model_copy(update={"google_oauth_client_id": None, "google_oauth_client_secret": None})
    monkeypatch.setattr(google, "get_settings", lambda: gone)
    monkeypatch.setattr(google, "_form", lambda *a, **k: pytest.fail("called Google"))
    response = client.get("/integrations/google/callback", params={"code": "c", "state": "state-1"},
                          headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 503
