"""OpenTelemetry initialization. Defaults to no-op when otel_endpoint is unset."""

from __future__ import annotations

import logging

from eidolon_agent.config.settings import ObservabilitySettings

_log = logging.getLogger(__name__)


def configure_tracing(settings: ObservabilitySettings) -> None:
    if not settings.otel_enabled or not settings.otel_endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _log.warning("opentelemetry packages not installed; tracing disabled")
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    _log.info("OTLP tracing → %s", settings.otel_endpoint)
