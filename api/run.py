"""Process entry point for the API server — what launchd actually runs.

`uvicorn api.main:app` on the command line would work, but its
`--log-config` flag takes a path to a JSON/YAML file, so using it would
mean keeping a second, hand-maintained copy of `uvicorn_log_config` on
disk and hoping the two stayed in step. Running uvicorn programmatically
keeps one source of truth.

Install: see ops/launchd/README.md.
"""

import uvicorn

from api.config import get_settings
from api.logging_config import uvicorn_log_config


def uvicorn_options(settings) -> dict:
    return {
        "app": "api.main:app",
        # Explicit, never uvicorn's default. The decisions-log entry "No
        # authentication, bound to loopback only" makes this the real
        # access boundary for the whole system, and a default is a weaker
        # guarantee than an argument.
        "host": "127.0.0.1",
        "port": settings.port,
        "log_config": uvicorn_log_config(settings),
        # launchd's KeepAlive already restarts this service; uvicorn's
        # reloader would be a second supervisor for the same process.
        "reload": False,
        # No WebSocket routes exist, and uvicorn logs every handshake through
        # uvicorn.error with its path AND query string, which is exported
        # (api/telemetry.py). Off, so there's nothing to log.
        "ws": "none",
    }


def _private_socket_directory(path) -> None:
    """The Serve socket's directory: created 0700 if absent; otherwise it must
    be owned by this user and closed to group and others, or startup stops.

    That directory is the whole trust boundary for identity headers: only
    tailscaled (root) and this user can reach the socket inside it.
    """
    import os
    import stat

    directory = path.parent
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit(f"serve_socket directory {directory} must be owned by this user with mode 0700")


def listening_sockets(settings) -> list:
    """TCP 127.0.0.1:port, plus the Serve Unix socket when configured.

    Tailscale Serve proxies to the socket; api/serve_gate.py treats every
    request that arrives there as coming from the tailnet.
    """
    import socket
    from pathlib import Path

    sockets = []
    try:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sockets.append(tcp)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind(("127.0.0.1", settings.port))
        if settings.serve_socket:
            path = Path(settings.serve_socket)
            _private_socket_directory(path)
            if path.is_socket():
                path.unlink()  # stale, from a previous run
            unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sockets.append(unix)
            unix.bind(str(path))
            path.chmod(0o600)
    except BaseException:
        for sock in sockets:
            sock.close()
        raise
    return sockets


def main() -> None:
    import logging

    from api import telemetry
    from api.main import app

    settings = get_settings()
    options = uvicorn_options(settings)
    sockets = listening_sockets(settings) if settings.serve_socket else None
    # After Config: constructing it installs the JSON file handler (a record
    # logged before that has nowhere to go) and replaces the root logger's
    # handlers, which would remove telemetry's log handler if it came first.
    config = uvicorn.Config(**options)
    if telemetry.configure(app):
        logging.getLogger("api").info("telemetry: exporting traces and logs over OTLP")
    if sockets is None:
        uvicorn.Server(config).run()
        return
    logging.getLogger("api").info("listening on 127.0.0.1:%s and Serve socket %s", settings.port, settings.serve_socket)
    uvicorn.Server(config).run(sockets=sockets)


if __name__ == "__main__":
    main()
