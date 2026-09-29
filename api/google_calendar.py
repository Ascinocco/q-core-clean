"""One-way Google Calendar projection for q-core reminders."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4
from threading import RLock
from functools import wraps
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException

from api.auth import require_token
from api.config import get_settings
from api.db import get_connection

router = APIRouter(dependencies=[Depends(require_token)])
callback_router = APIRouter()
SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
REDIRECT_PATH = "/integrations/google/callback"
KEYCHAIN_SERVICE = "q-core.google-calendar"
CLIENT_KEYCHAIN_SERVICE = "q-core.google-oauth-client"
_states: set[str] = set()
_projection_lock = RLock()


def serialized_reminder_write(function):
    """Order local writes and their projection within the single API process."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with _projection_lock:
            return function(*args, **kwargs)

    return wrapped


def _redirect_uri() -> str:
    """Where Google sends the browser after consent.

    Through Tailscale Serve when a Serve hostname is configured (the server):
    any browser on the tailnet can finish the sign-in, and the callback is
    in the Serve browser surface for the allowlisted identity. Otherwise the
    local service port, for a Mac/launchd instance. The same value is sent
    again at code exchange, so it must match the OAuth client's registered
    redirect exactly (runbooks/google-calendar.md).
    """
    settings = get_settings()
    if settings.serve_hostname:
        return f"https://{settings.serve_hostname}{REDIRECT_PATH}"
    return f"http://localhost:{settings.port}{REDIRECT_PATH}"


class CredentialsUnavailable(RuntimeError):
    """Google OAuth client credentials or the refresh token are not stored."""


def _token_store() -> str:
    """`keychain` (macOS, the original store) or `file` (everything else).

    The file store keeps the refresh token in a private file under
    `settings.secrets_dir` (0600 in a 0700 directory, owned by the service
    user), which the host backs up with its data (restic, client-side
    encrypted, the owner's decision 2026-09-25).
    """
    configured = get_settings().google_token_store
    if configured:
        return configured
    return "keychain" if sys.platform == "darwin" else "file"


def _token_file() -> Path:
    return Path(get_settings().secrets_dir) / "google-refresh-token"


def _request(
    url: str, method: str = "GET", body: dict | None = None, token: str | None = None
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=20) as response:  # nosec B310: fixed Google endpoints
        data = response.read()
        return json.loads(data) if data else {}


def _form(url: str, values: dict) -> dict:
    request = Request(
        url,
        data=urlencode(values).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=20) as response:  # nosec B310: fixed Google endpoint
        return json.loads(response.read())


def _store_refresh_token(token: str) -> None:
    if _token_store() == "file":
        _write_private_file(_token_file(), token)
        return
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            "refresh-token",
            "-w",
            token,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_private_file(path: Path, value: str) -> None:
    """Atomically replace `path` with `value`: 0600 file in a 0700 directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)  # created 0600
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    # The rename is durable only once the directory entry is: without this a
    # power cut just after sign-in can lose the token (the server must come back
    # from one exactly where it was).
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _keychain(service: str, account: str) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise CredentialsUnavailable("Google OAuth credentials are not configured") from None
    return result.stdout.strip()


def _client_credentials() -> tuple[str, str]:
    """From settings (on the server: sops); the Keychain only for the macOS store."""
    settings = get_settings()
    client_id, client_secret = settings.google_oauth_client_id, settings.google_oauth_client_secret
    if client_id and client_secret:
        return client_id, client_secret
    if _token_store() != "keychain":
        raise CredentialsUnavailable("Google OAuth credentials are not configured")
    return (client_id or _keychain(CLIENT_KEYCHAIN_SERVICE, "client-id"),
            client_secret or _keychain(CLIENT_KEYCHAIN_SERVICE, "client-secret"))


NOT_CONNECTED = "Google Calendar is not connected; run the sign-in"


def _refresh_token() -> str:
    """The stored refresh token. A missing one reads the same from either store."""
    if _token_store() == "file":
        try:
            token = _token_file().read_text().strip()
        except FileNotFoundError:
            raise CredentialsUnavailable(NOT_CONNECTED) from None
        if not token:
            raise CredentialsUnavailable(NOT_CONNECTED)
        return token
    try:
        return _keychain(KEYCHAIN_SERVICE, "refresh-token")
    except CredentialsUnavailable:
        raise CredentialsUnavailable(NOT_CONNECTED) from None


def _access_token() -> str:
    client_id, client_secret = _client_credentials()
    return _form(
        "https://oauth2.googleapis.com/token",
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": _refresh_token(),
            "grant_type": "refresh_token",
        },
    )["access_token"]


def _event(row: dict) -> dict:
    offsets = json.loads(row["notification_offsets_json"])
    event = {
        "summary": row["title"],
        "description": row["notes"] or "",
        "location": row["location"] or "",
        "extendedProperties": {"private": {"q_core_reminder_id": row["id"]}},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": value} for value in offsets],
        },
    }
    if row["due_time"]:
        start = datetime.fromisoformat(f"{row['start_date']}T{row['due_time']}")
        event["start"] = {
            "dateTime": start.isoformat(),
            "timeZone": (
                "America/New_York"
                if row.get("source_kind") == "birthday"
                else get_settings().timezone
            ),
        }
        event["end"] = {
            "dateTime": (start + timedelta(hours=1)).isoformat(),
            "timeZone": (
                "America/New_York"
                if row.get("source_kind") == "birthday"
                else get_settings().timezone
            ),
        }
    else:
        event["start"] = {"date": row["start_date"]}
        event["end"] = {
            "date": (
                date.fromisoformat(row["start_date"]) + timedelta(days=1)
            ).isoformat()
        }
    if row["recurrence_rule"]:
        rule = row["recurrence_rule"].removeprefix("RRULE:")
        # Cap the series without extending an earlier UNTIL or COUNT boundary.
        parts = dict(part.split("=", 1) for part in rule.split(";"))
        # q-core date rules are parsed against a naive midnight DTSTART.
        # Google requires a UTC UNTIL for a timed recurrence, and a date for
        # all-day recurrence. Preserve the inclusive local calendar date.
        if "UNTIL" in parts:
            last_date = date.fromisoformat(parts["UNTIL"][:8])
            if row["due_time"]:
                zone = ZoneInfo(event["start"]["timeZone"])
                parts["UNTIL"] = (
                    datetime.combine(last_date, datetime.max.time(), zone)
                    .astimezone(timezone.utc)
                    .strftime("%Y%m%dT%H%M%SZ")
                )
            else:
                parts["UNTIL"] = last_date.strftime("%Y%m%d")
        if row["end_date"]:
            from api.due import _occurrences

            last_day = date.fromisoformat(row["end_date"])
            if "COUNT" in parts:
                parts["COUNT"] = str(
                    len(
                        _occurrences(
                            row, date.fromisoformat(row["start_date"]), last_day
                        )
                    )
                )
            else:
                bound = last_day.strftime("%Y%m%d")
                if row["due_time"]:
                    zone = ZoneInfo(event["start"]["timeZone"])
                    bound = (
                        datetime.combine(last_day, datetime.max.time(), zone)
                        .astimezone(timezone.utc)
                        .strftime("%Y%m%dT%H%M%SZ")
                    )
                old = parts.get("UNTIL")
                parts["UNTIL"] = min(old, bound) if old else bound
        event["recurrence"] = [
            "RRULE:" + ";".join(f"{key}={value}" for key, value in parts.items())
        ]
    return event


def queue_reminder_deletion(connection, reminder_id: str) -> None:
    """Part of the caller's transaction, before cascading mapping deletion."""
    connection.execute(
        "INSERT OR IGNORE INTO google_calendar_deletions (calendar_id, event_id) "
        "SELECT c.calendar_id, e.event_id FROM google_calendar_connection c "
        "CROSS JOIN (SELECT event_id FROM google_calendar_events WHERE reminder_id = ? "
        "UNION SELECT event_id FROM google_calendar_occurrences WHERE reminder_id = ?) e",
        (reminder_id, reminder_id),
    )


def _delete_event(url, token):
    try:
        _request(url, "DELETE", token=token)
    except HTTPError as exc:
        if exc.code not in (404, 410):
            raise


def _flush_deletions(connection, token):
    for item in connection.execute(
        "SELECT * FROM google_calendar_deletions"
    ).fetchall():
        _delete_event(
            f"https://www.googleapis.com/calendar/v3/calendars/{item['calendar_id']}/events/{item['event_id']}",
            token,
        )
        connection.execute(
            "DELETE FROM google_calendar_deletions WHERE calendar_id = ? AND event_id = ?",
            (item["calendar_id"], item["event_id"]),
        )
        connection.commit()


def _project_event(connection, reminder_id, occurrence, payload, calendar_id, token):
    table = (
        "google_calendar_events"
        if occurrence is None
        else "google_calendar_occurrences"
    )
    clause = "reminder_id = ?" + (" AND due_date = ?" if occurrence is not None else "")
    key = (reminder_id,) if occurrence is None else (reminder_id, occurrence)
    mapped = connection.execute(
        f"SELECT event_id FROM {table} WHERE {clause}", key
    ).fetchone()
    root = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
    if payload is None:
        if mapped:
            _delete_event(f"{root}/{mapped['event_id']}", token)
            connection.execute(f"DELETE FROM {table} WHERE {clause}", key)
            connection.commit()
        return
    if not mapped:
        event_id = uuid4().hex
        columns = (
            "reminder_id, event_id"
            if occurrence is None
            else "reminder_id, due_date, event_id"
        )
        values = (*key, event_id)
        connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})",
            values,
        )
        # Persist the provider id BEFORE sending; a timeout can be retried without
        # creating a second remote event. Google accepts caller-supplied hex ids.
        connection.commit()
    else:
        event_id = mapped["event_id"]
        try:
            result = _request(f"{root}/{event_id}", "PUT", payload, token)
            if result.get("status") != "cancelled":
                return
        except HTTPError as exc:
            if exc.code not in (404, 410):
                raise
            # A 404 may mean a previous insert timed out before reaching Google.
            if exc.code == 404:
                return _insert_event(root, event_id, payload, token)
        # A tombstoned Google id cannot be reused after completion or remote deletion.
        event_id = uuid4().hex
        connection.execute(
            f"UPDATE {table} SET event_id = ? WHERE {clause}", (event_id, *key)
        )
        connection.commit()
    _insert_event(root, event_id, payload, token)


def _insert_event(root, event_id, payload, token):
    try:
        _request(root, "POST", {**payload, "id": event_id}, token)
    except HTTPError as exc:
        if exc.code != 409:
            raise
        # A previous insert succeeded but its response was lost.
        _request(f"{root}/{event_id}", "PUT", payload, token)


@serialized_reminder_write
def sync_reminder(connection, reminder_id: str) -> None:
    calendar = connection.execute(
        "SELECT calendar_id FROM google_calendar_connection WHERE id = 1"
    ).fetchone()
    if calendar is None:
        return
    token, calendar_id = _access_token(), calendar["calendar_id"]
    _flush_deletions(connection, token)
    stored = connection.execute(
        "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()
    if stored is None:
        return
    row = dict(stored)
    instances = connection.execute(
        "SELECT * FROM reminder_instances WHERE reminder_id = ?", (reminder_id,)
    ).fetchall()
    payload = _event(row)
    moved = {}
    if row["recurrence_rule"]:
        for instance in instances:
            if instance["status"] not in ("done", "skipped", "snoozed"):
                continue
            original = date.fromisoformat(instance["due_date"]).strftime("%Y%m%d")
            if row["due_time"]:
                original += "T" + row["due_time"].replace(":", "")
                exclusion = f"EXDATE;TZID={payload['start']['timeZone']}:{original}"
            else:
                exclusion = f"EXDATE;VALUE=DATE:{original}"
            payload["recurrence"].append(exclusion)
            if instance["status"] == "snoozed":
                moved[instance["due_date"]] = _event(
                    {
                        **row,
                        "start_date": instance["snoozed_to"],
                        "recurrence_rule": None,
                    }
                )
    elif instances:
        instance = instances[0]
        if instance["status"] in ("done", "skipped"):
            payload = None
        elif instance["status"] == "snoozed":
            payload = _event({**row, "start_date": instance["snoozed_to"]})
    _project_event(connection, reminder_id, None, payload, calendar_id, token)
    existing = {
        r["due_date"]
        for r in connection.execute(
            "SELECT due_date FROM google_calendar_occurrences WHERE reminder_id = ?",
            (reminder_id,),
        )
    }
    for occurrence in sorted(existing | moved.keys()):
        _project_event(
            connection,
            reminder_id,
            occurrence,
            moved.get(occurrence),
            calendar_id,
            token,
        )


def sync_reminder_safely(connection, reminder_id):
    try:
        sync_reminder(connection, reminder_id)
    except Exception as exc:
        connection.rollback()
        connection.execute(
            "UPDATE google_calendar_connection SET last_error = ? WHERE id = 1",
            (f"Reminder projection pending sync: {type(exc).__name__}",),
        )
        connection.commit()


@router.get("/integrations/google/status")
def status(connection=Depends(get_connection)) -> dict:
    # calendar_id is a provider routing capability and account_email is PII;
    # neither is needed to answer whether the projection is healthy. Keep both
    # server-side instead of returning them through the MCP status tool.
    row = connection.execute(
        "SELECT connected_at, last_synced_at, last_error "
        "FROM google_calendar_connection WHERE id = 1"
    ).fetchone()
    return {
        "connected": row is not None,
        "calendar": "q-core Reminders" if row is not None else None,
        "connection": dict(row) if row else None,
    }


@router.post("/integrations/google/connect")
def connect() -> dict:
    try:
        client_id, _ = _client_credentials()
    except CredentialsUnavailable as exc:
        raise HTTPException(503, "Google OAuth credentials are not configured") from exc
    state = secrets.token_urlsafe(32)
    _states.add(state)
    return {
        "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": _redirect_uri(),
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    }


@callback_router.get("/integrations/google/callback")
def callback(code: str, state: str, connection=Depends(get_connection)) -> dict:
    if state not in _states:
        raise HTTPException(400, "OAuth state is invalid or expired")
    _states.remove(state)
    try:
        client_id, client_secret = _client_credentials()
    except CredentialsUnavailable as exc:
        # Credentials removed between connect and the redirect: same answer
        # as connect gives, not a 500.
        raise HTTPException(503, "Google OAuth credentials are not configured") from exc
    tokens = _form(
        "https://oauth2.googleapis.com/token",
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        },
    )
    _store_refresh_token(tokens["refresh_token"])
    calendar = _request(
        "https://www.googleapis.com/calendar/v3/calendars",
        "POST",
        {"summary": "q-core Reminders"},
        tokens["access_token"],
    )
    # `keychain_service` is historical: it records the token STORE, the
    # Keychain service name on macOS or "file" (runbooks/google-calendar.md).
    # Kept rather than renamed: a rename is a migration for a label.
    connection.execute(
        "INSERT OR REPLACE INTO google_calendar_connection (id, calendar_id, keychain_service) VALUES (1, ?, ?)",
        (calendar["id"], KEYCHAIN_SERVICE if _token_store() == "keychain" else "file"),
    )
    connection.commit()
    return {"connected": True, "calendar": "q-core Reminders"}


@router.post("/integrations/google/sync")
@serialized_reminder_write
def sync_all(connection=Depends(get_connection)) -> dict:
    rows = connection.execute("SELECT id FROM reminders").fetchall()
    try:
        if connection.execute(
            "SELECT 1 FROM google_calendar_connection WHERE id = 1"
        ).fetchone():
            _flush_deletions(connection, _access_token())
        for row in rows:
            sync_reminder(connection, row["id"])
    except Exception as exc:
        connection.rollback()
        connection.execute(
            "UPDATE google_calendar_connection SET last_error = ? WHERE id = 1",
            (f"Reminder projection pending sync: {type(exc).__name__}",),
        )
        connection.commit()
        raise HTTPException(
            503,
            "Calendar sync failed; local reminders are saved. Retry sync_google_calendar.",
        ) from exc
    connection.execute(
        "UPDATE google_calendar_connection SET last_synced_at = CURRENT_TIMESTAMP, last_error = NULL WHERE id = 1"
    )
    connection.commit()
    return {"synced": len(rows)}
