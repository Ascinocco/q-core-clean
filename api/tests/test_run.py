"""Tests for api/run.py — the process entry point launchd invokes.

This module exists because uvicorn's CLI `--log-config` takes a *file*
path, while `uvicorn_log_config` is a dict. Rather than keep a duplicate
JSON copy of the logging config on disk and let the two drift, the plist
runs `python -m api.run`, which passes the dict directly.
"""

from api.run import uvicorn_options


def test_binds_loopback_only(test_settings):
    """The loopback bind is a security property, not a default to inherit.

    See runbooks/decisions-log.md, "No authentication, bound to loopback
    only": there is no auth beyond a shared-secret tripwire, so the bind
    address is the actual access boundary.
    """
    assert uvicorn_options(test_settings)["host"] == "127.0.0.1"


def test_uses_the_configured_port(test_settings):
    options = uvicorn_options(test_settings.model_copy(update={"port": 9999}))

    assert options["port"] == 9999


def test_passes_the_json_log_config_through(test_settings):
    options = uvicorn_options(test_settings)

    from api.logging_config import _HANDLER_TAG

    handler = options["log_config"]["handlers"][_HANDLER_TAG]
    assert handler["filename"].endswith("api.log")


def test_serves_the_real_app(test_settings):
    assert uvicorn_options(test_settings)["app"] == "api.main:app"


def test_does_not_enable_reload(test_settings):
    """--reload runs a supervisor plus a child process; under launchd's
    KeepAlive that is two restart mechanisms fighting over one service."""
    assert uvicorn_options(test_settings).get("reload", False) is False


def test_the_listening_line_reaches_the_log_file(test_settings, monkeypatch):
    """Logged after uvicorn.Config installs the file handler, not before it."""
    import json
    import logging
    import shutil
    import socket
    import tempfile
    from pathlib import Path

    import uvicorn

    import api.run
    from api.logging_config import log_path

    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {name: (lg.handlers[:], lg.level, lg.propagate)
             for name, lg in [(n, logging.getLogger(n)) for n in names]}
    saved_root = (logging.root.handlers[:], logging.root.level)
    directory = Path(tempfile.mkdtemp(prefix="qc-", dir="/tmp"))  # AF_UNIX path length
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); free = probe.getsockname()[1]; probe.close()
    settings = test_settings.model_copy(update={"port": free, "serve_socket": str(directory / "run" / "serve.sock")})
    monkeypatch.setattr(api.run, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: [s.close() for s in sockets])
    try:
        api.run.main()
        for handler in logging.root.handlers:
            handler.flush()
        lines = [json.loads(line) for line in log_path(settings).read_text().splitlines()]
        assert any(line["message"].startswith(f"listening on 127.0.0.1:{free}") for line in lines)
    finally:
        new = [h for h in logging.root.handlers if h not in saved_root[0]]
        for handler in new:
            handler.close()
        logging.root.handlers[:], _ = saved_root[0], logging.root.setLevel(saved_root[1])
        for name, (handlers, level, propagate) in saved.items():
            lg = logging.getLogger(name)
            lg.handlers[:], lg.level, lg.propagate = handlers, level, propagate
        shutil.rmtree(directory, ignore_errors=True)


def test_websockets_are_off(test_settings):
    """uvicorn logs WebSocket handshakes with the query string via an exported
    logger (review of q-core #12, R1-F1); there are no WebSocket routes."""
    assert uvicorn_options(test_settings)["ws"] == "none"
