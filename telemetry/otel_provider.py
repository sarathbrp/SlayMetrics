"""OpenTelemetry provider — drop-in replacement for LangfuseClient.

Exports traces to Grafana Tempo (via OTLP), metrics to Prometheus,
and logs to Loki. Same context-manager API as LangfuseClient.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator

from core import log as logger


def _safe_str(value: Any, max_len: int = 4096) -> str:
    """Convert value to a string safe for OTel attributes."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)[:max_len]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:max_len]
    except Exception:
        return str(value)[:max_len]


class OTelProvider:
    """OpenTelemetry observability provider.

    Provides the same API as LangfuseClient so orchestrator and agent
    code works without changes.
    """

    def __init__(self, config: dict[str, Any]):
        self._config = config
        self._enabled = bool(config.get("enabled", True))
        self._service_name = config.get("service_name", "slaymetrics")
        self._endpoint = config.get("endpoint", "http://localhost:4318")
        self._trace_content = config.get("trace_content", True)
        self._last_trace_url: str | None = None

        if not self._enabled:
            self._tracer = None
            self._meter = None
            return

        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry import metrics as otel_metrics
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            resource = Resource.create({
                "service.name": self._service_name,
                "service.version": "3.0.0",
            })

            # Traces → Tempo
            self._tracer_provider = TracerProvider(resource=resource)
            span_exporter = OTLPSpanExporter(endpoint=f"{self._endpoint}/v1/traces")
            self._tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
            otel_trace.set_tracer_provider(self._tracer_provider)
            self._tracer = otel_trace.get_tracer("slaymetrics")

            # Metrics → Prometheus
            try:
                metric_exporter = OTLPMetricExporter(endpoint=f"{self._endpoint}/v1/metrics")
                metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=30000)
                self._meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
                otel_metrics.set_meter_provider(self._meter_provider)
                self._meter = otel_metrics.get_meter("slaymetrics")
            except Exception:
                self._meter = None
                self._meter_provider = None

            # Custom metrics
            if self._meter:
                self._rps_gauge = self._meter.create_gauge(
                    "slaymetrics.benchmark.rps",
                    description="Benchmark requests per second",
                )
                self._tokens_counter = self._meter.create_counter(
                    "slaymetrics.llm.tokens",
                    description="LLM tokens consumed",
                )
                self._params_counter = self._meter.create_counter(
                    "slaymetrics.params.applied",
                    description="Parameters applied to DUT",
                )
            else:
                self._rps_gauge = None
                self._tokens_counter = None
                self._params_counter = None

            # Auto-instrument LangChain if available
            if config.get("auto_instrument_langchain", True):
                try:
                    from opentelemetry.instrumentation.langchain import LangchainInstrumentor
                    LangchainInstrumentor().instrument()
                    logger.status("otel", "LangChain auto-instrumentation enabled")
                except ImportError:
                    pass

            logger.status("otel", f"OpenTelemetry initialized → {self._endpoint}")

        except ImportError as e:
            logger.log(
                "otel",
                f"OpenTelemetry packages not installed ({e}). "
                "Install: pip install opentelemetry-api opentelemetry-sdk "
                "opentelemetry-exporter-otlp-proto-http",
                "warn",
            )
            self._enabled = False
            self._tracer = None
            self._meter = None

    @property
    def enabled(self) -> bool:
        return self._enabled and self._tracer is not None

    @property
    def last_trace_url(self) -> str | None:
        return self._last_trace_url

    def auth_check(self) -> bool:
        return self.enabled

    # ── Context managers (same API as LangfuseClient) ────────────────────

    @contextmanager
    def trace(
        self, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None
    ) -> Iterator[Any]:
        if not self.enabled:
            yield None
            return
        from opentelemetry import trace as otel_trace

        with self._tracer.start_as_current_span(name) as span:
            if metadata:
                for k, v in metadata.items():
                    span.set_attribute(f"slaymetrics.{k}", _safe_str(v))
            if input and self._trace_content:
                span.set_attribute("slaymetrics.input", _safe_str(input))
            yield span

    @contextmanager
    def span(
        self, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None
    ) -> Iterator[Any]:
        if not self.enabled:
            yield None
            return

        with self._tracer.start_as_current_span(name) as span:
            if metadata:
                for k, v in metadata.items():
                    span.set_attribute(f"slaymetrics.{k}", _safe_str(v))
            if input and self._trace_content:
                span.set_attribute("slaymetrics.input", _safe_str(input))
            yield span

    @contextmanager
    def tool_span(
        self, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None
    ) -> Iterator[Any]:
        with self.span(name, input=input, metadata=metadata) as s:
            yield s

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model_parameters: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        if not self.enabled:
            yield None
            return
        from opentelemetry.trace import SpanKind

        with self._tracer.start_as_current_span(name, kind=SpanKind.CLIENT) as span:
            span.set_attribute("gen_ai.system", "llm")
            span.set_attribute("gen_ai.request.model", str(model or "unknown"))
            if model_parameters:
                for k, v in model_parameters.items():
                    span.set_attribute(f"gen_ai.request.{k}", _safe_str(v))
            if input and self._trace_content:
                span.set_attribute("gen_ai.prompt", _safe_str(input, max_len=8192))
            if metadata:
                for k, v in metadata.items():
                    span.set_attribute(f"slaymetrics.{k}", _safe_str(v))
            yield span

    # ── Update methods ───────────────────────────────────────────────────

    def update_generation(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        usage_details: dict[str, int] | None = None,
    ) -> None:
        if not self.enabled:
            return
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if not span or not span.is_recording():
            return
        if output and self._trace_content:
            span.set_attribute("gen_ai.completion", _safe_str(output, max_len=8192))
        if usage_details:
            for k, v in usage_details.items():
                span.set_attribute(f"gen_ai.usage.{k}", int(v))
            # Record token metric
            total = sum(usage_details.values())
            if self._tokens_counter and total:
                self._tokens_counter.add(total, {"model": span.attributes.get("gen_ai.request.model", "unknown")})

    def update_span(self, *, output: Any = None) -> None:
        if not self.enabled:
            return
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span and span.is_recording() and output:
            span.set_attribute("slaymetrics.output", _safe_str(output))

    # ── Events ───────────────────────────────────────────────────────────

    def event(
        self,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if not span or not span.is_recording():
            return
        attrs: dict[str, str] = {}
        if input:
            attrs["input"] = _safe_str(input)
        if output:
            attrs["output"] = _safe_str(output)
        if metadata:
            for k, v in metadata.items():
                attrs[k] = _safe_str(v)
        span.add_event(name, attributes=attrs)

    # ── Custom metrics ───────────────────────────────────────────────────

    def record_benchmark(self, workload: str, rps: float, p99: float = 0.0) -> None:
        """Record benchmark result as a metric."""
        if self._rps_gauge:
            self._rps_gauge.set(rps, {"workload": workload})

    def record_params_applied(self, scope: str, count: int) -> None:
        """Record number of parameters applied."""
        if self._params_counter:
            self._params_counter.add(count, {"scope": scope})

    # ── Lifecycle ────────────────────────────────────────────────────────

    def flush(self) -> None:
        if hasattr(self, "_tracer_provider"):
            try:
                self._tracer_provider.force_flush()
            except Exception:
                pass
        if hasattr(self, "_meter_provider") and self._meter_provider:
            try:
                self._meter_provider.force_flush()
            except Exception:
                pass

    def shutdown(self) -> None:
        self.flush()
        if hasattr(self, "_tracer_provider"):
            try:
                self._tracer_provider.shutdown()
            except Exception:
                pass
        if hasattr(self, "_meter_provider") and self._meter_provider:
            try:
                self._meter_provider.shutdown()
            except Exception:
                pass
