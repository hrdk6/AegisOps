"""OpenTelemetry tracing setup for AegisOps' own components (self-observability)."""

from __future__ import annotations

import os


def setup_tracing(service: str, endpoint: str) -> None:
    """Export spans over OTLP/HTTP when an endpoint is configured; otherwise a no-op."""
    if not endpoint or os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service, "service.namespace": "aegisops"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
