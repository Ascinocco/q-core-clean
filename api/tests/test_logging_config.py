"""Tests for api/logging_config.py — the API server's JSON-lines logging.

The point of this logging is that a Claude session debugging a problem for
The owner reads `data/logs/api.log` off disk instead of asking them to reproduce
anything. That only works if every line is independently parseable, so most
of these tests parse the emitted line with `json.loads` rather than
asserting on substrings.
"""

import json
import logging
from pathlib import Path

import pytest

from api.logging_config import JsonFormatter, request_id_var


def _format(record: logging.LogRecord) -> dict:
    """Run one record through JsonFormatter and parse the result."""
    return json.loads(JsonFormatter().format(record))


def _record(**kwargs) -> logging.LogRecord:
    defaults = dict(
        name="api.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    defaults.update(kwargs)
    return logging.LogRecord(**defaults)


def test_formats_record_as_a_single_parseable_json_line():
    payload = JsonFormatter().format(_record())

    assert "\n" not in payload, "a JSON-lines record must not span lines"
    assert json.loads(payload)["message"] == "hello"


def test_includes_timestamp_level_and_logger_name():
    payload = _format(_record(name="api.request", level=logging.WARNING))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "api.request"
    # Parseable as a real timestamp, not just present.
    assert payload["timestamp"].endswith("+00:00")


def test_renders_printf_style_args_into_the_message():
    payload = _format(_record(msg="imported %d rows", args=(7,)))

    assert payload["message"] == "imported 7 rows"


def test_omits_request_id_when_there_is_none():
    assert "request_id" not in _format(_record())


def test_includes_request_id_from_the_context_var():
    token = request_id_var.set("abc123")
    try:
        payload = _format(_record())
    finally:
        request_id_var.reset(token)

    assert payload["request_id"] == "abc123"


def test_includes_extra_fields_passed_by_the_caller():
    record = _record()
    record.method = "GET"
    record.path = "/entities"
    record.status = 200
    record.duration_ms = 12.5

    payload = _format(record)

    assert payload["method"] == "GET"
    assert payload["path"] == "/entities"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 12.5


def test_includes_the_traceback_when_an_exception_is_attached():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = _record(level=logging.ERROR, exc_info=sys.exc_info())

    payload = _format(record)

    assert "RuntimeError: boom" in payload["traceback"]
    # Still one line: the traceback's own newlines must be JSON-escaped, not
    # emitted raw, or every log line after an error becomes unparseable.
    assert "\n" not in JsonFormatter().format(record)


def test_falls_back_to_a_repr_for_values_json_cannot_serialize():
    """A non-serializable `extra` must not take the whole log line down.

    Losing one field is recoverable; a TypeError inside the logging path
    means the record is dropped entirely and the thing being debugged
    leaves no trace.
    """
    record = _record()
    record.entity = object()

    payload = _format(record)

    assert "object object at" in payload["entity"]


@pytest.mark.parametrize("reserved", ["message", "timestamp", "level", "logger"])
def test_caller_extras_cannot_overwrite_the_reserved_fields(reserved):
    record = _record(msg="real message", name="api.real")
    setattr(record, reserved, "hijacked")

    payload = _format(record)

    assert payload[reserved] != "hijacked"


# --- configure_logging -------------------------------------------------


@pytest.fixture()
def isolated_root():
    """Run a test against a clean root logger, then restore the real one.

    configure_logging attaches to the root logger so that every library's
    loggers (uvicorn's included) land in the same file. That makes it
    global state, so each test has to put it back.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved_handlers, saved_level


def test_configure_logging_writes_json_lines_to_the_settings_log_dir(
    isolated_root, test_settings
):
    from api.logging_config import configure_logging

    configure_logging(test_settings)
    logging.getLogger("api.test").info("wrote a line")

    log_file = Path(test_settings.logs_dir) / "api.log"
    line = json.loads(log_file.read_text().splitlines()[0])
    assert line["message"] == "wrote a line"
    assert line["logger"] == "api.test"


def test_configure_logging_creates_the_log_directory_if_absent(
    isolated_root, test_settings
):
    from api.logging_config import configure_logging

    assert not Path(test_settings.logs_dir).exists()

    configure_logging(test_settings)

    assert Path(test_settings.logs_dir).is_dir()


def test_configure_logging_never_attaches_a_stdout_or_stderr_handler(
    isolated_root, test_settings
):
    """The API's own reason is modest (launchd captures stdout anyway), but
    the MCP server shares this design and there a stdout write corrupts the
    protocol stream. Same rule both sides, asserted on both sides."""
    import sys

    from api.logging_config import configure_logging

    configure_logging(test_settings)

    streams = [
        getattr(handler, "stream", None) for handler in logging.getLogger().handlers
    ]
    assert sys.stdout not in streams
    assert sys.stderr not in streams


def _our_handlers() -> list[logging.Handler]:
    """Just the handlers this module installed.

    pytest's logging plugin attaches its own LogCaptureHandler to the root
    logger for every test, so "the root logger has one handler" is not a
    true statement to assert even when configure_logging is behaving.
    """
    from api.logging_config import _HANDLER_TAG

    return [h for h in logging.getLogger().handlers if h.get_name() == _HANDLER_TAG]


def test_configure_logging_rotates_at_ten_megabytes_with_five_backups(
    isolated_root, test_settings
):
    from api.logging_config import configure_logging

    configure_logging(test_settings)

    (handler,) = _our_handlers()
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5


def test_configure_logging_twice_does_not_duplicate_handlers(
    isolated_root, test_settings
):
    """Uvicorn's reloader and the test suite both import the app more than
    once; duplicated handlers would write every line twice."""
    from api.logging_config import configure_logging

    configure_logging(test_settings)
    configure_logging(test_settings)

    assert len(_our_handlers()) == 1


def test_configure_logging_honours_the_configured_level(isolated_root, test_settings):
    from api.logging_config import configure_logging

    configure_logging(test_settings.model_copy(update={"log_level": "WARNING"}))
    logging.getLogger("api.test").info("should not appear")
    logging.getLogger("api.test").warning("should appear")

    lines = (Path(test_settings.logs_dir) / "api.log").read_text().splitlines()
    assert [json.loads(line)["message"] for line in lines] == ["should appear"]


def test_log_level_is_configurable_by_environment_variable(monkeypatch):
    """The owner changes verbosity without editing code: Q_CORE_LOG_LEVEL=DEBUG."""
    from api.config import Settings

    monkeypatch.setenv("Q_CORE_API_TOKEN", "env-token")
    monkeypatch.setenv("Q_CORE_LOG_LEVEL", "DEBUG")

    assert Settings(_env_file=None).log_level == "DEBUG"


def test_logs_dir_defaults_under_data():
    from api.config import REPO_ROOT, Settings

    settings = Settings(_env_file=None, api_token="test-token")

    assert settings.logs_dir == str(REPO_ROOT / "data" / "logs")
    assert settings.log_level == "INFO"
