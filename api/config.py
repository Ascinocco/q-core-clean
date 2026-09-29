from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


#: The settings' environment prefix.
ENV_PREFIX = "Q_CORE_"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_prefix=ENV_PREFIX,
        extra="ignore",
    )

    api_token: str
    db_path: str = str(REPO_ROOT / "data" / "q-core.db")
    documents_dir: str = str(REPO_ROOT / "data" / "documents")
    jyra_dir: str = str(REPO_ROOT / "data" / "jyra")
    eval_summaries_dir: str = str(REPO_ROOT / "data" / "evals" / "published")
    forecast_dir: str = str(REPO_ROOT / "data" / "forecast")
    schema_path: str = str(REPO_ROOT / "db" / "schema.sql")
    seed_categories_path: str = str(REPO_ROOT / "db" / "seed_categories.sql")
    migrations_dir: str = str(REPO_ROOT / "db" / "migrations")
    logs_dir: str = str(REPO_ROOT / "data" / "logs")
    # Documents staged for intake. Read-only to the API: the extract
    # endpoint is the only thing that opens anything here.
    intake_dir: str = str(REPO_ROOT / "intake")
    # Transient staging for mobile capture. Documents registered from here
    # are MOVED out; see api/documents.py's move-vs-copy note.
    inbox_dir: str = str(REPO_ROOT / "inbox")
    # Private, operator-reviewed names/address variants; never returned by API.
    privacy_profile_path: str = str(REPO_ROOT / "data" / "privacy" / "redaction.json")
    #: Where attach_file may read from. A POSITIVE allow-list with exactly
    #: one entry by default: a directory that exists for this purpose and
    #: holds nothing else.
    #:
    #: It defaulted to the working tree, with holes for the financial
    #: surface. review-1 found what that leaves: `attach_file(ticket,
    #: ".env")` copies Q_CORE_API_TOKEN verbatim into a store a model can
    #: read back, and `logs/api.log`, `.git/config` and `data/jyra/` with
    #: it. That is the deny-list #70 removed -- it must enumerate every
    #: dangerous thing, and I had enumerated only the financial ones.
    #:
    #: So the default names what is ALLOWED rather than what is refused.
    #: Widening it -- to the Desktop, or anywhere else -- is a deliberate
    #: act by someone who can see what they are permitting.
    attachment_roots: tuple[str, ...] = (
        str(REPO_ROOT / "data" / "attachments-inbox"),
    )
    # Raise to DEBUG with Q_CORE_LOG_LEVEL=DEBUG when a report needs more
    # detail than INFO carries; no code edit and no restart of anything but
    # the server itself.
    log_level: str = "INFO"
    port: int = 8420
    # Tailscale Serve proxies to this Unix socket (split, part 2), e.g.
    # /run/q-core/serve.sock. Its directory must be 0700 and owned by the
    # service user, so only tailscaled (root) and this user can connect:
    # identity headers on it were set by Serve. Requests on it need an
    # allowlisted identity (browser pages) or a token (api/serve_gate.py).
    # None: no Serve listener.
    serve_socket: str | None = Field(default=None, pattern=r"^/[^\x00]+\.sock$")
    # The Tailscale Serve DNS name (e.g. q-core.tail1234.ts.net). Serve keeps
    # the client's Host header, and the MCP transport's DNS-rebinding guard
    # answers 421 to any Host it was not told about, so MCP through Serve
    # needs this. None: MCP accepts loopback hosts only, as before.
    serve_hostname: str | None = Field(default=None, pattern=r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+ts\.net$")
    # Tailscale logins (e.g. user@github) allowed to open the browser pages
    # through Serve. Empty means no identity is accepted: tokens only.
    ui_allowed_logins: list[str] = Field(default_factory=list)
    transcription_enabled: bool = False
    transcription_token: str | None = None
    whisper_port: int = Field(default=18542, ge=1024, le=65535)
    transcription_ffmpeg: str = "/opt/homebrew/bin/ffmpeg"
    # Where the Google refresh token lives: "keychain" (macOS) or "file"
    # (a 0600 file in secrets_dir). None: keychain on macOS, file elsewhere.
    google_token_store: Literal["keychain", "file"] | None = None
    # Private credentials the app writes itself (the Google refresh token on
    # Linux). 0700, owned by the service user; backed up with the data.
    secrets_dir: str = str(REPO_ROOT / "data" / "secrets")
    # The private data directory as a whole. Nothing writes to it by this
    # name; attach_file refuses everything inside it (except configured
    # attachment roots) whatever attachment_roots says. On an installed
    # app REPO_ROOT is the read-only Nix store, so the service sets this.
    data_dir: str = str(REPO_ROOT / "data")
    # The secrets file the service manager loads (systemd EnvironmentFile),
    # when there is one besides .env. attach_file refuses it by identity.
    environment_file: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    timezone: str = "UTC"


@lru_cache
def get_settings() -> Settings:
    return Settings()
