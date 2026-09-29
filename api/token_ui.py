"""Browser page for client API tokens: create, list, revoke (split, part 2).

Managing tokens mints credentials, so it is the one browser surface held to a
stricter rule than the read-only pages:

- **Only a person through Tailscale Serve.** The JSON endpoints answer only a
  request that arrived on the Serve port carrying an allowlisted Tailscale
  identity (api/serve_gate.py). A bearer token of any scope cannot manage
  tokens here (a phone's transcription token must not mint a full one), and
  the loopback service port, where local processes need no credential,
  refuses outright. On the server itself, `python -m api.tokens` is the way.
- **Writes are pinned to the configured Serve origin.** The request's Host
  must be `settings.serve_hostname` and its Origin must be present and equal
  `https://<serve_hostname>`. Nothing is derived from the request's own Host,
  so a DNS-rebinding page (Host = Origin = its own name) is refused, and with
  no serve_hostname configured every write is refused. The custom header and
  JSON body also keep a cross-site form from reaching them.

The page itself (static HTML under a strict CSP) is a browser page like the
others (require_reader); on the service port its script's first request is
refused with a pointer to the CLI.
"""
import base64
import hashlib
import re
import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api import tokens
from api.auth import require_reader
from api.config import Settings, get_settings
from api.db import get_connection
from api.serve_gate import identity_allowed, request_came_through_serve
from api.ui import STATIC, render_page

router = APIRouter()

WRITE_HEADER = "X-Q-Core-Request"
WRITE_HEADER_VALUE = "tokens"
NO_STORE = {"Cache-Control": "no-store"}


def _sha256(script: str) -> str:
    return base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()


def _inline_scripts(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


# Computed at import from the static files, so an edit that breaks
# extraction fails every test at import rather than serving a 500.
_PAGE_SCRIPTS = _inline_scripts((STATIC / "tokens.html").read_text())
if len(_PAGE_SCRIPTS) != 1:
    raise RuntimeError("tokens.html must contain exactly one inline <script> block")
SCRIPT_HASHES = (_sha256((STATIC / "shell.js").read_text()), _sha256(_PAGE_SCRIPTS[0]))
CSP = (
    "default-src 'none'; script-src " + " ".join(f"'sha256-{h}'" for h in SCRIPT_HASHES) + "; "
    "style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)


class CreateToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    scope: str = "full"


class RevokeToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)


def _refuse(message: str):
    raise HTTPException(403, detail={"error": {"code": "forbidden", "message": message}})


def _person_through_serve(request: Request, settings: Settings = Depends(get_settings)) -> Settings:
    if not request_came_through_serve(request.scope, settings):
        _refuse("Tokens are managed through Tailscale Serve, or with `python -m api.tokens` on the server")
    headers = dict(request.scope.get("headers") or ())
    if not identity_allowed(headers, settings):
        _refuse("Token management needs an allowed Tailscale identity; API tokens cannot manage tokens")
    return settings


def _pinned_origin_write(
    request: Request,
    settings: Settings = Depends(_person_through_serve),
    x_q_core_request: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> None:
    if x_q_core_request != WRITE_HEADER_VALUE:
        _refuse(f"Token changes must come from the token page ({WRITE_HEADER} header missing)")
    if not (content_type or "").startswith("application/json"):
        _refuse("Token changes must be sent as JSON")
    expected = settings.serve_hostname
    if not expected:
        _refuse("Token changes need serve_hostname configured")
    if request.headers.get("host", "").lower() not in {expected, expected + ":443"}:
        _refuse("Token changes must be addressed to the configured Serve name")
    if request.headers.get("origin", "").lower() != "https://" + expected:
        _refuse("Token changes must come from the configured Serve origin")


@router.get("/ui/tokens", include_in_schema=False, dependencies=[Depends(require_reader)])
def tokens_page():
    response = render_page((STATIC / "tokens.html").read_text(), "/ui/tokens")
    response.headers["Content-Security-Policy"] = CSP
    return response


@router.get("/ui/tokens/list", include_in_schema=False, dependencies=[Depends(_person_through_serve)])
def list_page_tokens(connection: sqlite3.Connection = Depends(get_connection)):
    return {"items": tokens.list_tokens(connection)}


@router.post("/ui/tokens/create", include_in_schema=False, dependencies=[Depends(_pinned_origin_write)])
def create_page_token(body: CreateToken, connection: sqlite3.Connection = Depends(get_connection)):
    try:
        secret, record = tokens.create(connection, body.name, body.scope)
    except ValueError as exc:
        raise HTTPException(422, detail={"error": {"code": "validation_error", "message": str(exc)}}) from None
    # The one response that carries a secret: never cached.
    return JSONResponse({"token": secret, "record": record}, headers=NO_STORE)


@router.post("/ui/tokens/revoke", include_in_schema=False, dependencies=[Depends(_pinned_origin_write)])
def revoke_page_token(body: RevokeToken, connection: sqlite3.Connection = Depends(get_connection)):
    return {"record": tokens.revoke(connection, body.id, id_only=True)}
