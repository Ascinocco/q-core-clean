"""Tests for routing uvicorn's own logging into `data/logs/api.log`.

Scope item: uvicorn's logs must reach disk rather than only stdout, so that
a crash at startup (a port already bound, a missing token) leaves a record
even when nothing is watching the terminal.

One deliberate deviation, measured rather than assumed: uvicorn's *access*
logger is switched off instead of routed in, because it renders the query
string into every line. See `test_the_access_logger_is_silenced` below.
"""

import json
import logging
import logging.config
from pathlib import Path

import pytest

from api.logging_config import uvicorn_log_config


@pytest.fixture()
def applied_config(test_settings):
    """dictConfig the real uvicorn config, then put logging back."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        logging.config.dictConfig(uvicorn_log_config(test_settings))
        yield
    finally:
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            for handler in logger.handlers:
                handler.close()
            logger.handlers, logger.propagate, logger.level = [], True, logging.NOTSET
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved_handlers, saved_level


def _log_text(test_settings) -> str:
    log_file = Path(test_settings.logs_dir) / "api.log"
    return log_file.read_text() if log_file.exists() else ""


def test_uvicorn_error_logs_reach_the_file_as_json(applied_config, test_settings):
    logging.getLogger("uvicorn.error").warning("could not bind to port 8420")

    line = json.loads(_log_text(test_settings).splitlines()[0])
    assert line["message"] == "could not bind to port 8420"
    assert line["logger"] == "uvicorn.error"
    assert line["level"] == "WARNING"


def test_uvicorn_tracebacks_reach_the_file(applied_config, test_settings):
    """A startup crash is the case where stdout is least likely to be read."""
    try:
        raise OSError("address already in use")
    except OSError:
        logging.getLogger("uvicorn.error").exception("startup failed")

    line = json.loads(_log_text(test_settings).splitlines()[0])
    assert "OSError: address already in use" in line["traceback"]


def test_the_access_logger_is_silenced(applied_config, test_settings):
    """uvicorn's access line contains the query string by construction.

    `uvicorn.protocols.utils.get_path_with_query_string` renders
    `/search?q=...`, so routing this logger into the file would write query
    strings to disk — against CLAUDE.md's rule that account-number-shaped
    data never gets stored. The middleware in api/logging_config.py already
    logs every request with method, path, status, duration and request_id,
    and without the query string, so nothing is lost by silencing this.
    """
    logging.getLogger("uvicorn.access").info(
        '127.0.0.1:0 - "GET /search?q=123456789 HTTP/1.1" 200'
    )

    assert "123456789" not in _log_text(test_settings)


def test_no_handler_writes_to_stdout_or_stderr(applied_config, test_settings):
    import sys

    configured = [logging.getLogger()] + [
        logging.getLogger(name)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    streams = [
        getattr(handler, "stream", None)
        for logger in configured
        for handler in logger.handlers
    ]

    assert sys.stdout not in streams
    assert sys.stderr not in streams


def test_the_config_is_a_valid_uvicorn_log_config(test_settings):
    """uvicorn passes this straight to logging.config.dictConfig, so a
    malformed dict fails at server start — the worst time to find out."""
    config = uvicorn_log_config(test_settings)

    assert config["version"] == 1
    assert config["disable_existing_loggers"] is False


def test_a_line_is_written_once_when_both_configs_are_applied(
    applied_config, test_settings
):
    """The real startup path applies both: uvicorn reads --log-config at
    boot, then the app's lifespan calls configure_logging.

    Found by running the server for real rather than by a unit test — every
    `api.*` line landed in api.log twice, because each config had installed
    its own file handler on the root logger. uvicorn's own loggers were
    single because they set propagate: False, which is exactly what made
    the duplication easy to miss.
    """
    from api.logging_config import configure_logging

    configure_logging(test_settings)
    logging.getLogger("api.request").info("GET /health 200")

    lines = _log_text(test_settings).splitlines()
    messages = [json.loads(line)["message"] for line in lines]
    assert messages.count("GET /health 200") == 1


def test_uvicorns_color_message_extra_is_dropped(applied_config, test_settings):
    """uvicorn attaches `color_message` — the same text with ANSI colour
    escapes — to several of its records. It is meant for a terminal, and in
    a JSON file it is unreadable noise duplicating `message`."""
    logging.getLogger("uvicorn.error").info(
        "Started server process [%d]",
        4242,
        extra={"color_message": "Started server process [\x1b[36m%d\x1b[0m]"},
    )

    line = json.loads(_log_text(test_settings).splitlines()[0])
    assert "color_message" not in line
    assert line["message"] == "Started server process [4242]"
