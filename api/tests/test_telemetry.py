"""OpenTelemetry: off by default, and nothing but allowlisted fields leaves.

Every value below that looks sensitive is invented. The privacy rule is
CLAUDE.md's "Safety and operation"; api/telemetry.py explains the allowlist.
"""
import json
import logging

import anyio
import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from api import telemetry

AUTH = {"Authorization": "Bearer test-token"}
FAKE_CARD = "4532015112830366"
FAKE_NAME = "Invented Personname"


def test_off_unless_an_endpoint_is_configured():
    assert not telemetry.enabled({})
    assert telemetry.enabled({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"})
    assert telemetry.enabled({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:4318/v1/traces"})
    assert not telemetry.enabled({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x", "OTEL_SDK_DISABLED": "true"})


def test_configure_without_an_endpoint_changes_nothing():
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"configure touched app.{name}")

    assert telemetry.configure(Untouchable(), environ={}) is False


def test_the_allowlists_are_pinned():
    """Adding a key is a privacy decision: change this test deliberately."""
    assert telemetry.SPAN_ATTRIBUTES == {
        "http.request.method", "http.method", "http.response.status_code", "http.status_code",
        "http.route", "error.type",
        "mcp.method.name", "mcp.protocol.version", "gen_ai.operation.name",
        "mcp.tool.name", "mcp.outcome",
        "q_core.file.kind", "q_core.pages", "q_core.redactions", "q_core.partial",
        "q_core.rows", "q_core.needs_review", "q_core.source_conflicts",
        "q_core.created", "q_core.linked", "q_core.recognized",
        "q_core.audio.media_type", "q_core.audio.bytes", "q_core.audio.seconds",
        "q_core.outcome",
    }
    assert telemetry.EVENT_ATTRIBUTES == {"exception.type"}
    assert telemetry.LOG_ATTRIBUTES == {"method", "path", "status", "duration_ms", "request_id", "exception.type"}
    assert telemetry.LOG_LOGGERS == {"api", "api.request", "api.error", "api.documents", "uvicorn.error"}


@pytest.fixture(scope="module")
def exported():
    """Telemetry on, once per process (the global tracer provider can be set
    only once), exporting to memory. Taken out again afterwards."""
    from api.main import app

    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    assert telemetry.configure(app, span_exporter=spans, log_exporter=logs)
    yield spans, logs
    telemetry.unconfigure()
    trace.get_tracer_provider().shutdown()


@pytest.fixture()
def fresh(exported):
    spans, logs = exported
    spans.clear()
    logs.clear()
    return spans, logs


def _assert_allowlisted(spans):
    assert spans, "nothing was exported"
    for span in spans:
        assert set(span.attributes) <= telemetry.SPAN_ATTRIBUTES, span.name
        for event in span.events:
            assert set(event.attributes) <= telemetry.EVENT_ATTRIBUTES, (span.name, event.name)
        assert span.status.description is None, span.name
        dumped = span.to_json()
        assert FAKE_CARD not in dumped and FAKE_NAME not in dumped and "Personname" not in dumped


def test_a_request_is_traced_with_its_route_and_without_its_query_string(fresh, client):
    spans, _ = fresh
    response = client.get(f"/entities?name={FAKE_NAME}&note={FAKE_CARD}", headers=AUTH)
    assert response.status_code in (200, 422)
    exported = spans.get_finished_spans()
    _assert_allowlisted(exported)
    server = [s for s in exported if s.kind == trace.SpanKind.SERVER]
    assert [s.name for s in server] == ["GET /entities"]
    assert server[0].attributes["http.route"] == "/entities"


def test_a_parameterized_route_is_named_by_its_template(fresh, client):
    spans, _ = fresh
    client.get("/entities/00000000-0000-4000-8000-000000000000", headers=AUTH)
    names = [s.name for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
    assert names == ["GET /entities/{entity_id}"]


def test_exception_messages_and_stack_traces_are_not_exported(fresh):
    spans, _ = fresh
    with pytest.raises(ValueError):
        with telemetry.tracer.start_as_current_span("probe", attributes={
            "q_core.rows": 3, "db.statement": f"SELECT '{FAKE_CARD}'", "http.url": f"/x?q={FAKE_NAME}",
        }):
            raise ValueError(f"card {FAKE_CARD} for {FAKE_NAME}")
    (span,) = spans.get_finished_spans()
    _assert_allowlisted([span])
    assert span.attributes == {"q_core.rows": 3}
    assert [dict(e.attributes) for e in span.events] == [{"exception.type": "ValueError"}]
    assert span.status.status_code == trace.StatusCode.ERROR


def test_an_mcp_tool_call_is_one_span_named_for_the_registered_tool(fresh, test_settings):
    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    spans, _ = fresh
    server = build_server(QCoreClient(test_settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0}))))
    anyio.run(lambda: server.call_tool("list_boards", {}))
    with pytest.raises(Exception):
        anyio.run(lambda: server.call_tool(f"no_such_tool_{FAKE_NAME}", {}))

    tool_spans = [s for s in spans.get_finished_spans() if s.name.startswith("mcp.tool ")]
    _assert_allowlisted(tool_spans)
    assert [(s.name, s.attributes["mcp.outcome"]) for s in tool_spans] == [
        ("mcp.tool list_boards", "ok"), ("mcp.tool unknown", "error"),
    ]
    assert tool_spans[1].attributes["mcp.tool.name"] == "unknown"


def test_the_sdk_mcp_span_keeps_only_its_method(exported):
    """The SDK names its span "tools/call <name as the client sent it>"."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(telemetry.AllowlistSpanExporter(memory)))
    sdk_tracer = provider.get_tracer("mcp-python-sdk")
    with sdk_tracer.start_as_current_span(f"tools/call {FAKE_NAME}", attributes={
        "mcp.method.name": "tools/call", "gen_ai.tool.name": FAKE_NAME, "jsonrpc.request.id": "7",
    }):
        pass
    (span,) = memory.get_finished_spans()
    assert span.name == "tools/call"
    assert dict(span.attributes) == {"mcp.method.name": "tools/call"}


def test_exported_logs_keep_allowlisted_fields_and_a_scrubbed_message(fresh):
    _, logs = fresh
    logger = logging.getLogger("api.request")
    previous = logger.level
    logger.setLevel(logging.INFO)  # as in production; setLevel also clears the level cache
    try:
        _log_probe(logger)
    finally:
        logger.setLevel(previous)
    (record,) = [r.log_record for r in logs.get_finished_logs() if "GET /x" in str(r.log_record.body)]
    assert dict(record.attributes) == {"method": "GET", "path": "/x", "status": 200}
    assert FAKE_CARD not in record.body and record.body.endswith("****0366")


def _log_probe(logger):
    logger.info(
        f"GET /x 200 for {FAKE_CARD}",
        extra={"method": "GET", "path": "/x", "status": 200, "customer": FAKE_NAME},
    )


def test_file_logs_carry_the_trace_id_inside_a_span(exported):
    from api.logging_config import JsonFormatter

    record = logging.LogRecord("api", logging.INFO, __file__, 1, "inside", None, None)
    with telemetry.tracer.start_as_current_span("probe") as span:
        line = json.loads(JsonFormatter().format(record))
    assert line["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert "trace_id" not in json.loads(JsonFormatter().format(record))


def test_file_kind_is_a_closed_set():
    assert telemetry.file_kind("/intake/2026-09/statement.PDF") == "pdf"
    assert telemetry.file_kind(f"/intake/{FAKE_NAME}.xlsx") == "other"
    assert telemetry.file_kind("/intake/noext") == "other"


def test_an_extraction_span_carries_kind_and_counts_but_no_text(fresh, test_settings, tmp_path):
    from fastapi.testclient import TestClient

    from api.config import get_settings
    from api.main import app

    spans, _ = fresh
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "statement.txt").write_text(f"CAFE {FAKE_NAME} 5.00 card {FAKE_CARD}\n")
    settings = test_settings.model_copy(update={"intake_dir": str(intake)})
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = TestClient(app).post("/documents/extract", json={"path": "statement.txt"}, headers=AUTH)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    exported = spans.get_finished_spans()
    _assert_allowlisted(exported)
    (extract,) = [s for s in exported if s.name == "intake.extract"]
    assert dict(extract.attributes) == {
        "q_core.file.kind": "txt", "q_core.pages": 1, "q_core.redactions": 1, "q_core.partial": False,
    }


@pytest.mark.parametrize("name", ["httpx", "api.due", "some.library"])
def test_logs_from_other_loggers_are_not_exported(fresh, name):
    """httpx's INFO line is `HTTP Request: GET <full URL>`, query string and
    all (review of q-core #11); api.due quotes attribute values. Neither is
    a reviewed message, so neither leaves, whatever its level."""
    _, logs = fresh
    logger = logging.getLogger(name)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info(f'HTTP Request: GET http://127.0.0.1:8420/notes?q={FAKE_NAME} "HTTP/1.1 200 OK"')
    finally:
        logger.setLevel(previous)
    assert not [r for r in logs.get_finished_logs() if "Personname" in str(r.log_record.body)]


def test_httpx_request_lines_are_silenced_at_the_source(test_settings):
    """The same line would otherwise land in data/logs/api.log as well."""
    import logging.config

    from api.logging_config import uvicorn_log_config

    config = uvicorn_log_config(test_settings)
    assert config["loggers"]["httpx"]["level"] == "WARNING"
    assert config["loggers"]["httpcore"]["level"] == "WARNING"


def test_an_mcp_search_does_not_reach_any_log(fresh, test_settings, tmp_path, monkeypatch):
    """End to end: a tool call whose query carries a name, through a real
    httpx client, leaves no trace of the name in the file log or the export."""
    from api.logging_config import configure_logging
    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    _, logs = fresh
    settings = test_settings.model_copy(update={"logs_dir": str(tmp_path / "logs")})
    root = logging.getLogger()
    before = list(root.handlers), root.level
    configure_logging(settings)
    try:
        server = build_server(QCoreClient(settings, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0}))))
        anyio.run(lambda: server.call_tool("list_notes", {"q": FAKE_NAME}))
    finally:
        for handler in root.handlers:
            if handler not in before[0]:
                handler.flush()
    written = (tmp_path / "logs" / "api.log").read_text() if (tmp_path / "logs" / "api.log").exists() else ""
    try:
        assert "Personname" not in written
        assert not [r for r in logs.get_finished_logs() if "Personname" in str(r.log_record.body)]
    finally:
        for handler in list(root.handlers):
            if handler not in before[0]:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(before[1])


def test_a_client_supplied_tracestate_is_not_exported(fresh, client):
    spans, _ = fresh
    client.get("/entities", headers={
        **AUTH,
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": "x=InventedPersonname",
    })
    exported = spans.get_finished_spans()
    _assert_allowlisted(exported)
    for span in exported:
        assert not span.context.trace_state and not (span.parent and span.parent.trace_state)
    # The trace itself still joins the caller's.
    assert {format(s.context.trace_id, "032x") for s in exported} == {"0af7651916cd43dd8448eb211c80319c"}


@pytest.mark.parametrize("name", sorted(telemetry.LOG_LOGGERS))
def test_every_allowlisted_logger_is_exported_under_the_production_config(fresh, test_settings, tmp_path, name):
    """Under uvicorn's dictConfig, as api/run.py sets it up: that config
    stops uvicorn.error propagating, which once kept it from ever exporting."""
    import logging.config

    from api.logging_config import uvicorn_log_config

    _, logs = fresh
    watched = ["", *telemetry.LOG_LOGGERS, "uvicorn", "uvicorn.access", "httpx", "httpcore"]
    saved = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).level, logging.getLogger(n).propagate,
                 logging.getLogger(n).disabled) for n in watched}
    settings = test_settings.model_copy(update={"logs_dir": str(tmp_path / "logs")})
    try:
        logging.config.dictConfig(uvicorn_log_config(settings))
        telemetry.attach_log_handler(telemetry._installed["handler"])
        logging.getLogger(name).setLevel(logging.INFO)
        logging.getLogger(name).info("probe for %s", "export")
        exported = [r for r in logs.get_finished_logs() if r.log_record.body == "probe for export"]
        assert exported and exported[0].instrumentation_scope.name == name
    finally:
        for n, (handlers, level, propagate, disabled) in saved.items():
            logger = logging.getLogger(n)
            for handler in logger.handlers:
                if handler not in handlers:
                    handler.close()
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate, logger.disabled = propagate, disabled


def test_allowlisted_loggers_use_constant_message_templates():
    """An exported message is a fixed template with %-arguments, never an
    f-string or a concatenation, so what reaches Loki is reviewable here.

    Every logger binding must resolve: getLogger("name"), getLogger(__name__)
    (the module's dotted name), and `x = ...` / `self.x = ...` assignments of
    either. A getLogger call it can't resolve fails the test rather than
    being skipped (review of q-core #12, R1-F3).
    """
    import ast
    from pathlib import Path

    root = Path(telemetry.__file__).resolve().parent.parent
    levels = {"debug", "info", "warning", "error", "exception", "critical"}
    offenders, unresolved = [], []
    for path in [*root.joinpath("api").glob("*.py"), *root.joinpath("q_core_mcp").rglob("*.py")]:
        if "tests" in path.parts:
            continue
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        tree = ast.parse(path.read_text())
        bound = {}  # "x" or "self.x" -> logger name

        def key(node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                return f"{node.value.id}.{node.attr}"
            return None

        def resolve(node):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "getLogger":
                if not node.args:
                    return ""  # the root logger
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    return arg.value
                if isinstance(arg, ast.Name) and arg.id == "__name__":
                    return module
                unresolved.append(f"{path.relative_to(root)}:{node.lineno}")
                return None
            return bound.get(key(node))

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and key(node.targets[0]):
                name = resolve(node.value)
                if name is not None:
                    bound[key(node.targets[0])] = name
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            method = node.func.attr
            if method not in levels and method != "log":
                continue
            if resolve(node.func.value) not in telemetry.LOG_LOGGERS:
                continue
            args = node.args[1:] if method == "log" else node.args
            first = args[0] if args else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert unresolved == [], f"getLogger calls this test can't resolve: {unresolved}"
    assert offenders == [], f"non-constant templates: {offenders}"


def test_the_template_scan_catches_what_it_should(tmp_path, monkeypatch):
    """The scan itself: f-strings, .log(), __name__ and self.logger are caught."""
    fake = tmp_path / "api"
    fake.mkdir()
    (fake / "documents.py").write_text(
        "import logging\n"
        "log = logging.getLogger(__name__)\n"
        "class C:\n"
        "    def __init__(self):\n"
        "        self.logger = logging.getLogger('api')\n"
        "    def f(self, name):\n"
        "        log.info(f'hello {name}')\n"
        "        self.logger.log(20, 'x ' + name)\n"
        "        logging.getLogger('api.request').info('%s ok', name)\n"
    )
    (tmp_path / "q_core_mcp").mkdir()
    monkeypatch.setattr(telemetry, "__file__", str(fake / "telemetry.py"))
    with pytest.raises(AssertionError) as excinfo:
        test_allowlisted_loggers_use_constant_message_templates()
    assert "api/documents.py:7" in str(excinfo.value) and "api/documents.py:8" in str(excinfo.value)
    assert "api/documents.py:9" not in str(excinfo.value)
