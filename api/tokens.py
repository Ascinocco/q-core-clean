"""Named, scoped, revocable API tokens for client machines (split, part 2).

Two kinds of credential reach the API:

- **The service token**, `settings.api_token`, is server configuration. The
  in-process MCP server uses it for its own loopback calls, so it is always
  accepted and never stored here. Retiring the old shared value means
  rotating it in the server's `.env`, not revoking a row.
- **Client tokens** live in `api_tokens`: one per machine or device, with a
  scope, created and revoked by name. Only a SHA-256 of each is stored; the
  secret is shown once, at creation. They are high-entropy random strings,
  so a fast hash is the right tool (no password stretching needed).

Scopes: `full` may use every authenticated route and MCP; `transcription`
may use only `/transcriptions` (the phone).

    python -m api.tokens create NAME [--scope full|transcription]
    python -m api.tokens list
    python -m api.tokens revoke NAME
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from api.errors import ConflictError, NotFoundError

SCOPES = ("full", "transcription")
TOKEN_PREFIX = "qc_"
#: Characters of the token shown in listings, enough to tell tokens apart.
DISPLAY_CHARS = 10
#: last_used_at is only rewritten when older than this, so every request
#: does not become a write.
LAST_USED_RESOLUTION = timedelta(seconds=60)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _public(row: sqlite3.Row) -> dict:
    """A token as listings show it: never the secret or its hash."""
    return {"id": row["id"], "name": row["name"], "scope": row["scope"], "prefix": row["prefix"],
            "created_at": row["created_at"], "last_used_at": row["last_used_at"],
            "revoked_at": row["revoked_at"]}


def create(connection: sqlite3.Connection, name: str, scope: str = "full") -> tuple[str, dict]:
    """Create a token. Returns (secret, public record); the secret is not stored."""
    name = name.strip()
    if not name or len(name) > 80:
        raise ValueError("token name must be 1-80 characters")
    if scope not in SCOPES:
        raise ValueError("scope must be full or transcription")
    if connection.execute("SELECT 1 FROM api_tokens WHERE name = ? AND revoked_at IS NULL", (name,)).fetchone():
        raise ConflictError("An active token already has that name; revoke it or choose another name.")
    secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = str(uuid4())
    try:
        connection.execute(
            "INSERT INTO api_tokens (id, name, scope, token_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, name, scope, token_hash(secret), secret[:DISPLAY_CHARS], _now()),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        # Another create won the race between the check above and this
        # insert; the partial unique index on active names refused ours.
        connection.rollback()
        raise ConflictError("An active token already has that name; revoke it or choose another name.") from None
    row = connection.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
    return secret, _public(row)


def list_tokens(connection: sqlite3.Connection, include_revoked: bool = True) -> list[dict]:
    query = "SELECT * FROM api_tokens"
    if not include_revoked:
        query += " WHERE revoked_at IS NULL"
    query += " ORDER BY revoked_at IS NOT NULL, created_at, id"
    return [_public(row) for row in connection.execute(query)]


def revoke(connection: sqlite3.Connection, name_or_id: str, *, id_only: bool = False) -> dict:
    """Revoke an active token. The CLI accepts a name or id; the page sends ids only."""
    if id_only:
        query, params = "SELECT * FROM api_tokens WHERE id = ? AND revoked_at IS NULL", (name_or_id,)
    else:
        query = "SELECT * FROM api_tokens WHERE (id = ? OR name = ?) AND revoked_at IS NULL"
        params = (name_or_id, name_or_id)
    row = connection.execute(query, params).fetchone()
    if row is None:
        raise NotFoundError("No active token matches that name or id")
    connection.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?", (_now(), row["id"]))
    connection.commit()
    return _public(connection.execute("SELECT * FROM api_tokens WHERE id = ?", (row["id"],)).fetchone())


def verify(connection: sqlite3.Connection, presented: str | None, *, allowed_scopes: tuple[str, ...]) -> bool:
    """Is `presented` (the raw Authorization header value) an acceptable client token?

    The service token is not checked here; api.auth.token_accepted compares
    it, in constant time, before ever opening a connection. A client token is found
    by its hash (an indexed lookup, so no timing oracle on the secret), must
    be unrevoked and must carry an allowed scope.
    """
    if not presented or not presented.startswith("Bearer "):
        return False
    token = presented[len("Bearer "):]
    if not token.startswith(TOKEN_PREFIX):
        return False
    row = connection.execute(
        "SELECT id, scope, last_used_at FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
        (token_hash(token),),
    ).fetchone()
    if row is None or row["scope"] not in allowed_scopes:
        return False
    now = datetime.now(timezone.utc)
    last = row["last_used_at"]
    if last is None or now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) >= LAST_USED_RESOLUTION:
        connection.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (_now(), row["id"]))
        connection.commit()
    return True


def open_connection(settings) -> sqlite3.Connection:
    """A short-lived connection for auth checks outside FastAPI dependencies."""
    from api.db import BUSY_TIMEOUT_SECONDS, init_db

    init_db(settings)
    connection = sqlite3.connect(settings.db_path, timeout=BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    return connection


def _scope_argument(value: str) -> str:
    if value not in SCOPES:
        raise argparse.ArgumentTypeError("scope must be full or transcription")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.tokens", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create", help="create a token and print its secret once")
    make.add_argument("name")
    make.add_argument("--scope", type=_scope_argument, default="full", help="full or transcription")
    commands.add_parser("list", help="list tokens (never shows secrets)")
    drop = commands.add_parser("revoke", help="revoke an active token by name or id")
    drop.add_argument("name")
    args = parser.parse_args(argv)

    from api.config import get_settings

    connection = open_connection(get_settings())
    try:
        if args.command == "create":
            secret, record = create(connection, args.name, args.scope)
            print(json.dumps({**record, "token": secret}, indent=2))
            print("Store this token now; it will not be shown again.", file=sys.stderr)
        elif args.command == "list":
            print(json.dumps(list_tokens(connection), indent=2))
        else:
            print(json.dumps(revoke(connection, args.name), indent=2))
        return 0
    except (ValueError, ConflictError, NotFoundError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
