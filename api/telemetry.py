"""OpenTelemetry for q-core: traces and logs to the server's observability stack.

Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` (or its traces-specific variant) is
set, and `OTEL_SDK_DISABLED` isn't "true". Everything else is the standard
`OTEL_*` environment: `OTEL_SERVICE_NAME` (default here "q-core"),
`OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_PROTOCOL`. Tests, the Mac's
launchd job and anything else without the variable get the API's no-op
tracer, so the explicit spans in the code cost nothing there.

`api/run.py` calls `configure(app)` before uvicorn starts. Importing
`api.main` stays free of side effects.

**Privacy (CLAUDE.md "Safety and operation").** No document text, financial
values, account or card numbers, emails, names, tokens or query strings may
reach a span or a log field. Instrumentation libraries decide what they
record, and they record URLs with query strings, exception messages and
stack traces. So nothing leaves through their choices: both exporters are
wrapped in an ALLOWLIST.
- Span and event attributes whose key isn't listed below are dropped.
- Status descriptions are dropped; they're exception messages. So is any
  client-supplied W3C tracestate.
- Exception events keep only `exception.type`.
- Log records are exported only from q-core's own reviewed loggers
  (`LOG_LOGGERS`), with only the listed attributes. Their body is the message
  the app already writes to `data/logs/api.log`, passed through the
  account-number scrubber as well.

Adding a key here is a privacy decision; `api/tests/test_telemetry.py` pins
the list.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from opentelemetry import trace

#: Span attributes that may be exported. Methods, status codes, routes
#: (templates, not URLs), tool names, outcomes, and counts, kinds and sizes.
SPAN_ATTRIBUTES = frozenset({
    # HTTP (both semantic-convention generations)
    "http.request.method", "http.method",
    "http.response.status_code", "http.status_code",
    "http.route",
    "error.type",
    # MCP: the SDK's own span and ours
    "mcp.method.name", "mcp.protocol.version",
    "gen_ai.operation.name",
    "mcp.tool.name", "mcp.outcome",
    # q-core's explicit spans: counts, kinds and sizes only
    "q_core.file.kind", "q_core.pages", "q_core.redactions", "q_core.partial",
    "q_core.rows", "q_core.needs_review", "q_core.source_conflicts",
    "q_core.created", "q_core.linked", "q_core.recognized",
    "q_core.audio.media_type", "q_core.audio.bytes", "q_core.audio.seconds",
    "q_core.outcome",
})

#: Attributes kept on span events. An exception event's message and
#: stack trace can carry any value the failing code held.
EVENT_ATTRIBUTES = frozenset({"exception.type"})

#: Log record attributes that may be exported: the request-log fields the
#: app already writes (runbooks/observability.md) and the correlation ids.
LOG_ATTRIBUTES = frozenset({
    "method", "path", "status", "duration_ms", "request_id", "exception.type",
})

#: Loggers whose records may be exported, by exact name. Their messages are
#: written by q-core and reviewed for this (runbooks/observability.md, "What
#: is deliberately *not* logged"). Everything else is dropped from export:
#: third-party libraries log what they like (httpx logs every request's full
#: URL, query string included), and `api.due` quotes unparseable attribute
#: values. Those still reach data/logs/api.log unless silenced there.
LOG_LOGGERS = frozenset({"api", "api.request", "api.error", "api.documents", "uvicorn.error"})

#: File kinds an extraction span may name; anything else is "other".
FILE_KINDS = frozenset({"pdf", "csv", "tsv", "txt"})

tracer = trace.get_tracer("q-core")

#: What configure() installed, so tests can take it out again.
_installed: dict = {}


def enabled(environ=os.environ) -> bool:
    if environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    return bool(environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))


def file_kind(path) -> str:
    suffix = str(path).rsplit(".", 1)[-1].lower() if "." in str(path) else ""
    return suffix if suffix in FILE_KINDS else "other"


def _without_trace_state(context):
    """A span context minus its W3C tracestate, which a client can send with
    any content (review of q-core #11, R1-F2). Ids and flags are kept."""
    from opentelemetry.trace import SpanContext, TraceState

    if context is None:
        return None
    return SpanContext(context.trace_id, context.span_id, context.is_remote,
                       context.trace_flags, TraceState())


def _filtered_span(span):
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace import Link, Status

    name = span.name
    scope = span.instrumentation_scope.name if span.instrumentation_scope else ""
    if scope == "mcp-python-sdk":
        # The SDK names its span "<method> <target>" with the target exactly as
        # the client sent it. Our own "mcp.tool <name>" span records only
        # registered tool names, so this one keeps just the method.
        name = str((span.attributes or {}).get("mcp.method.name") or "mcp")
    return ReadableSpan(
        name=name,
        context=_without_trace_state(span.context),
        parent=_without_trace_state(span.parent),
        resource=span.resource,
        attributes={k: v for k, v in (span.attributes or {}).items() if k in SPAN_ATTRIBUTES},
        events=[
            Event(event.name, {k: v for k, v in (event.attributes or {}).items() if k in EVENT_ATTRIBUTES},
                  event.timestamp)
            for event in span.events
        ],
        links=[Link(_without_trace_state(link.context)) for link in span.links],
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class AllowlistSpanExporter:
    """Wraps a SpanExporter; see the module docstring."""

    def __init__(self, inner):
        self._inner = inner

    def export(self, spans: Sequence):
        return self._inner.export([_filtered_span(span) for span in spans])

    def shutdown(self):
        return self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


class AllowlistLogProcessor:
    """Wraps a LogRecordProcessor, filtering each record before it's queued."""

    def __init__(self, inner):
        self._inner = inner

    def on_emit(self, record) -> None:
        from api.redaction import scrub_text

        scope = record.instrumentation_scope.name if record.instrumentation_scope else ""
        if scope not in LOG_LOGGERS:
            return
        log = record.log_record
        log.attributes = {k: v for k, v in (log.attributes or {}).items() if k in LOG_ATTRIBUTES}
        if log.body is not None:
            log.body = scrub_text(str(log.body)).text
        self._inner.on_emit(record)

    def shutdown(self):
        return self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


#: Allowlisted loggers that uvicorn's dictConfig sets to propagate=False, so
#: their records never reach a root handler. The export handler is attached
#: to them directly (review of q-core #11, R2-F1): their start-up,
#: bind-failure and traceback lines are the ones worth having in Loki.
_NON_PROPAGATING = ("uvicorn.error",)


def attach_log_handler(handler) -> None:
    """The export handler on the root logger and on each non-propagating
    allowlisted logger. The exporter's LOG_LOGGERS check still decides."""
    logging.getLogger().addHandler(handler)
    for name in _NON_PROPAGATING:
        logging.getLogger(name).addHandler(handler)


def configure(app, *, span_exporter=None, log_exporter=None, environ=os.environ) -> bool:
    """Install tracing and log export if the environment asks for it.

    The exporters are parameters so tests can pass in-memory ones; in
    production they're OTLP/HTTP, configured by the standard variables.
    Returns whether telemetry was turned on.
    """
    if span_exporter is None and not enabled(environ):
        return False

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    testing = span_exporter is not None
    if span_exporter is None:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter, log_exporter = OTLPSpanExporter(), OTLPLogExporter()

    # OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES are read by create();
    # the default below applies only when neither names the service.
    resource = Resource.create({"service.name": environ.get("OTEL_SERVICE_NAME") or "q-core"})
    span_processor = SimpleSpanProcessor if testing else BatchSpanProcessor
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(span_processor(AllowlistSpanExporter(span_exporter)))
    trace.set_tracer_provider(provider)

    if log_exporter is not None:
        log_processor = SimpleLogRecordProcessor if testing else BatchLogRecordProcessor
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(AllowlistLogProcessor(log_processor(log_exporter)))
        set_logger_provider(logger_provider)
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        attach_log_handler(handler)
        _installed["handler"] = handler
    _installed["app"] = app

    # Route templates as span names; the per-message ASGI send/receive spans
    # add nothing but volume.
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, exclude_spans=["receive", "send"])
    # Starlette builds its middleware stack on the first request. In production
    # configure() runs before that; resetting the stack makes it rebuild with
    # the instrumentation either way.
    app.middleware_stack = None
    # The MCP tools call the API over loopback httpx; this carries the trace
    # across, so a tool call and the request it makes are one trace.
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return True


def unconfigure() -> None:
    """Remove the instrumentation configure() added. For tests."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    handler = _installed.pop("handler", None)
    if handler is not None:
        for name in _NON_PROPAGATING:
            logging.getLogger(name).removeHandler(handler)
        logging.getLogger().removeHandler(handler)
    app = _installed.pop("app", None)
    if app is not None:
        FastAPIInstrumentor.uninstrument_app(app)
        app.middleware_stack = None
        HTTPXClientInstrumentor().uninstrument()
