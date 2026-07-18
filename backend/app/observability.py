"""Privacy-preserving, bounded process-local observability primitives.

The module deliberately has no application imports and exposes Prometheus text as
a function rather than a public route.  The ASGI middleware generates its own
request correlation identifier; untrusted inbound identifiers, request bodies,
query strings, prompts, capabilities, and environments are never retained.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from logging import Filter, LogRecord
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.responses import JSONResponse

REQUEST_ID_HEADER = b"x-request-id"
_REQUEST_ID_PATTERN = re.compile(r"^req_[a-f0-9]{24}$")
_JOB_HASH_KEY = secrets.token_bytes(32)
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_IMAGE_KINDS = frozenset({"planet", "surface", "inhabitant"})
_IMAGE_QUALITIES = frozenset({"fast", "balanced", "quality"})
_IMAGE_OUTCOMES = frozenset(
    {
        "accepted",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "rejected_queue",
        "rejected_storage",
    }
)
_IMAGE_PHASES = frozenset({"queued", "generating", "verifying", "refining", "upscaling"})
_PHASE_OUTCOMES = frozenset({"succeeded", "failed", "cancelled"})
_READINESS_CHECKS = frozenset(
    {"database", "analysis_provider", "storage", "frontend", "image_provider"}
)
_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "terra_request_id", default="-"
)
current_job_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "terra_job_correlation_id", default="-"
)
log = logging.getLogger("terra.observability")


def new_request_id() -> str:
    """Return a server-generated request identifier with no client-derived data."""
    return f"req_{secrets.token_hex(12)}"


def job_correlation_id(job_reference: str) -> str:
    """Derive a process-local log correlation value without exposing a job capability."""
    digest = hashlib.blake2s(
        job_reference.encode("utf-8", errors="replace"),
        key=_JOB_HASH_KEY,
        digest_size=10,
    ).hexdigest()
    return f"job_{digest}"


@contextmanager
def job_correlation(job_reference: str) -> Iterator[str]:
    """Bind a safe, non-reversible job correlation value for the current task."""
    correlation_id = job_correlation_id(job_reference)
    token = current_job_correlation_id.set(correlation_id)
    try:
        yield correlation_id
    finally:
        current_job_correlation_id.reset(token)


def correlation_fields() -> dict[str, str]:
    """Return fields suitable for a structured log record."""
    return {
        "request_id": current_request_id.get(),
        "job_correlation_id": current_job_correlation_id.get(),
    }


class CorrelationLogFilter(Filter):
    """Attach safe correlation fields to records without inspecting log messages."""

    def filter(self, record: LogRecord) -> bool:
        record.request_id = current_request_id.get()
        record.job_correlation_id = current_job_correlation_id.get()
        return True


@dataclass(slots=True)
class _Histogram:
    buckets: list[int] = field(default_factory=lambda: [0] * len(_LATENCY_BUCKETS))
    count: int = 0
    total: float = 0.0

    def observe(self, value: float) -> None:
        for index, boundary in enumerate(_LATENCY_BUCKETS):
            if value <= boundary:
                self.buckets[index] += 1
        self.count += 1
        self.total += value


class TerraMetrics:
    """Small fixed-purpose registry with explicit series and route bounds."""

    def __init__(self, *, max_routes: int = 64, max_request_series: int = 2048) -> None:
        self.max_routes = max(1, min(512, int(max_routes)))
        self.max_request_series = max(16, min(16_384, int(max_request_series)))
        self._lock = threading.RLock()
        self._known_routes: set[str] = set()
        self._request_counts: dict[tuple[str, str, str], int] = {}
        self._request_latency: dict[tuple[str, str], _Histogram] = {}
        self._requests_in_progress = 0
        self._image_jobs: dict[tuple[str, str, str], int] = {}
        self._image_phases: dict[tuple[str, str, str, str], int] = {}
        self._image_queue = {
            "active_jobs": 0,
            "active_work_units": 0,
            "max_jobs": 0,
            "max_work_units": 0,
        }
        self._cleanup_runs = {"success": 0, "degraded": 0, "failed": 0}
        self._cleanup_gauges: dict[str, float] = {}
        self._readiness: dict[str, float] = {}
        self._storage_gauges: dict[str, float] = {}
        self._process_start_time = time.time()

    def reset(self) -> None:
        """Clear collected samples. Intended for isolated tests and process reinitialization."""
        with self._lock:
            self._known_routes.clear()
            self._request_counts.clear()
            self._request_latency.clear()
            self._requests_in_progress = 0
            self._image_jobs.clear()
            self._image_phases.clear()
            self._image_queue.update(
                active_jobs=0,
                active_work_units=0,
                max_jobs=0,
                max_work_units=0,
            )
            self._cleanup_runs = {"success": 0, "degraded": 0, "failed": 0}
            self._cleanup_gauges.clear()
            self._readiness.clear()
            self._storage_gauges.clear()
            self._process_start_time = time.time()

    def begin_request(self) -> None:
        with self._lock:
            self._requests_in_progress += 1

    def record_request(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        method_label = method.upper() if method.upper() in _HTTP_METHODS else "OTHER"
        status_label = str(status_code) if 100 <= status_code <= 599 else "other"
        duration = duration_seconds if math.isfinite(duration_seconds) else 0.0
        duration = max(0.0, min(86_400.0, duration))
        with self._lock:
            route_label = self._bounded_route(route)
            count_key = (method_label, route_label, status_label)
            if (
                count_key not in self._request_counts
                and len(self._request_counts) >= self.max_request_series
            ):
                count_key = ("OTHER", "__overflow__", "other")
            self._request_counts[count_key] = self._request_counts.get(count_key, 0) + 1

            latency_key = (method_label, route_label)
            if (
                latency_key not in self._request_latency
                and len(self._request_latency) >= self.max_routes * 8
            ):
                latency_key = ("OTHER", "__overflow__")
            self._request_latency.setdefault(latency_key, _Histogram()).observe(duration)

    def end_request(self) -> None:
        with self._lock:
            self._requests_in_progress = max(0, self._requests_in_progress - 1)

    def record_image_job(self, *, kind: str, quality: str, outcome: str) -> None:
        key = (
            kind if kind in _IMAGE_KINDS else "other",
            quality if quality in _IMAGE_QUALITIES else "other",
            outcome if outcome in _IMAGE_OUTCOMES else "other",
        )
        with self._lock:
            self._image_jobs[key] = self._image_jobs.get(key, 0) + 1

    def record_image_phase(
        self,
        *,
        kind: str,
        quality: str,
        phase: str,
        outcome: str,
    ) -> None:
        key = (
            kind if kind in _IMAGE_KINDS else "other",
            quality if quality in _IMAGE_QUALITIES else "other",
            phase if phase in _IMAGE_PHASES else "other",
            outcome if outcome in _PHASE_OUTCOMES else "other",
        )
        with self._lock:
            self._image_phases[key] = self._image_phases.get(key, 0) + 1

    def set_image_queue(
        self,
        *,
        active_jobs: int,
        active_work_units: int,
        max_jobs: int,
        max_work_units: int,
    ) -> None:
        with self._lock:
            self._image_queue.update(
                active_jobs=_bounded_nonnegative(active_jobs),
                active_work_units=_bounded_nonnegative(active_work_units),
                max_jobs=_bounded_nonnegative(max_jobs),
                max_work_units=_bounded_nonnegative(max_work_units),
            )

    def record_cleanup(self, result: Any) -> None:
        """Record a cleanup result without retaining paths or error messages."""
        aborted = bool(getattr(result, "aborted", True))
        errors = getattr(result, "errors", ())
        limits_satisfied = bool(getattr(result, "limits_satisfied", False))
        if aborted:
            outcome = "failed"
        elif errors or not limits_satisfied:
            outcome = "degraded"
        else:
            outcome = "success"
        now = time.time()
        with self._lock:
            self._cleanup_runs[outcome] += 1
            self._cleanup_gauges.update(
                last_run_timestamp_seconds=now,
                last_duration_seconds=_bounded_float(
                    getattr(result, "elapsed_seconds", 0.0), maximum=86_400.0
                ),
                last_limits_satisfied=1.0 if limits_satisfied else 0.0,
                last_reclaimed_bytes=float(
                    _bounded_nonnegative(getattr(result, "reclaimed_bytes", 0))
                ),
                last_store_bytes=float(
                    _bounded_nonnegative(getattr(result, "store_bytes_after", 0))
                ),
            )
            if outcome == "success":
                self._cleanup_gauges["last_success_timestamp_seconds"] = now
            free_bytes = getattr(result, "estimated_free_bytes_after", None)
            if isinstance(free_bytes, int) and free_bytes >= 0:
                self._storage_gauges["free_bytes"] = float(free_bytes)
            self._storage_gauges["generated_store_bytes"] = float(
                _bounded_nonnegative(getattr(result, "store_bytes_after", 0))
            )

    def record_readiness(
        self,
        *,
        checks: Mapping[str, bool],
        free_disk_bytes: int | None = None,
    ) -> None:
        """Record only the fixed safe readiness dimensions used by Terra."""
        with self._lock:
            for name in _READINESS_CHECKS:
                if name in checks:
                    self._readiness[name] = 1.0 if checks[name] else 0.0
            if free_disk_bytes is not None:
                self._storage_gauges["free_bytes"] = float(
                    _bounded_nonnegative(free_disk_bytes)
                )

    def record_storage_free_bytes(self, free_bytes: int) -> None:
        with self._lock:
            self._storage_gauges["free_bytes"] = float(_bounded_nonnegative(free_bytes))

    def prometheus_text(self) -> str:
        """Render a consistent Prometheus 0.0.4 text snapshot."""
        with self._lock:
            request_counts = dict(self._request_counts)
            latency = {
                key: (tuple(value.buckets), value.count, value.total)
                for key, value in self._request_latency.items()
            }
            in_progress = self._requests_in_progress
            image_jobs = dict(self._image_jobs)
            image_phases = dict(self._image_phases)
            image_queue = dict(self._image_queue)
            cleanup_runs = dict(self._cleanup_runs)
            cleanup_gauges = dict(self._cleanup_gauges)
            readiness = dict(self._readiness)
            storage = dict(self._storage_gauges)
            process_start_time = self._process_start_time

        lines = [
            "# HELP terra_process_start_time_seconds Unix timestamp when metrics initialized.",
            "# TYPE terra_process_start_time_seconds gauge",
            f"terra_process_start_time_seconds {_number(process_start_time)}",
            "# HELP terra_http_requests_total HTTP requests by bounded route, method, and status.",
            "# TYPE terra_http_requests_total counter",
        ]
        for (method, route, status), value in sorted(request_counts.items()):
            labels = (
                f'method="{_escape(method)}",route="{_escape(route)}",'
                f'status="{_escape(status)}"'
            )
            lines.append(
                f"terra_http_requests_total{{{labels}}} {value}"
            )
        lines.extend(
            [
                "# HELP terra_http_requests_in_progress "
                "Requests currently executing in this process.",
                "# TYPE terra_http_requests_in_progress gauge",
                f"terra_http_requests_in_progress {in_progress}",
                "# HELP terra_http_request_duration_seconds End-to-end request latency.",
                "# TYPE terra_http_request_duration_seconds histogram",
            ]
        )
        for (method, route), (buckets, count, total) in sorted(latency.items()):
            label_prefix = f'method="{_escape(method)}",route="{_escape(route)}"'
            for boundary, value in zip(_LATENCY_BUCKETS, buckets, strict=True):
                lines.append(
                    "terra_http_request_duration_seconds_bucket"
                    f'{{{label_prefix},le="{_number(boundary)}"}} {value}'
                )
            lines.append(
                f'terra_http_request_duration_seconds_bucket{{{label_prefix},le="+Inf"}} {count}'
            )
            lines.append(
                f"terra_http_request_duration_seconds_count{{{label_prefix}}} {count}"
            )
            lines.append(
                f"terra_http_request_duration_seconds_sum{{{label_prefix}}} {_number(total)}"
            )

        lines.extend(
            [
                "# HELP terra_image_jobs_total Image job admission and terminal outcomes.",
                "# TYPE terra_image_jobs_total counter",
            ]
        )
        for (kind, quality, outcome), value in sorted(image_jobs.items()):
            labels = f'kind="{kind}",quality="{quality}",outcome="{outcome}"'
            lines.append(
                f"terra_image_jobs_total{{{labels}}} {value}"
            )
        lines.extend(
            [
                "# HELP terra_image_job_phase_outcomes_total Image pipeline phase exit outcomes.",
                "# TYPE terra_image_job_phase_outcomes_total counter",
            ]
        )
        for (kind, quality, phase, outcome), value in sorted(image_phases.items()):
            labels = (
                f'kind="{kind}",quality="{quality}",phase="{phase}",'
                f'outcome="{outcome}"'
            )
            lines.append(
                f"terra_image_job_phase_outcomes_total{{{labels}}} {value}"
            )
        for name, help_text in (
            ("active_jobs", "Active image jobs."),
            ("active_work_units", "Active image work units."),
            ("max_jobs", "Configured active image job limit."),
            ("max_work_units", "Configured image work unit limit."),
        ):
            metric_name = f"terra_image_queue_{name}"
            lines.extend(
                [
                    f"# HELP {metric_name} {help_text}",
                    f"# TYPE {metric_name} gauge",
                    f"{metric_name} {image_queue[name]}",
                ]
            )

        lines.extend(
            [
                "# HELP terra_generated_cleanup_runs_total Generated asset cleanup outcomes.",
                "# TYPE terra_generated_cleanup_runs_total counter",
            ]
        )
        for outcome, value in sorted(cleanup_runs.items()):
            lines.append(
                f'terra_generated_cleanup_runs_total{{outcome="{outcome}"}} {value}'
            )
        for name, value in sorted(cleanup_gauges.items()):
            metric_name = f"terra_generated_cleanup_{name}"
            lines.extend(
                [
                    f"# HELP {metric_name} Latest generated asset cleanup signal.",
                    f"# TYPE {metric_name} gauge",
                    f"{metric_name} {_number(value)}",
                ]
            )

        lines.extend(
            [
                "# HELP terra_readiness_check Readiness dependency state (1 ready, 0 not ready).",
                "# TYPE terra_readiness_check gauge",
            ]
        )
        for check, value in sorted(readiness.items()):
            lines.append(f'terra_readiness_check{{check="{check}"}} {_number(value)}')
        for name, value in sorted(storage.items()):
            metric_name = f"terra_storage_{name}"
            lines.extend(
                [
                    f"# HELP {metric_name} Latest bounded local storage signal.",
                    f"# TYPE {metric_name} gauge",
                    f"{metric_name} {_number(value)}",
                ]
            )
        return "\n".join(lines) + "\n"

    def _bounded_route(self, route: str) -> str:
        if route == "unmatched":
            return route
        normalized = route.strip()
        if not normalized.startswith("/") or len(normalized) > 160:
            return "other"
        if normalized in self._known_routes:
            return normalized
        if len(self._known_routes) >= self.max_routes:
            return "__overflow__"
        self._known_routes.add(normalized)
        return normalized


class RequestObservabilityMiddleware:
    """Generate safe request IDs and collect route-template request metrics."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics_registry: TerraMetrics | None = None,
        request_id_factory: Callable[[], str] = new_request_id,
    ) -> None:
        self.app = app
        self.metrics = metrics_registry or metrics
        self.request_id_factory = request_id_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        proposed_id = self.request_id_factory()
        request_id = proposed_id if _REQUEST_ID_PATTERN.fullmatch(proposed_id) else new_request_id()
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["request_id"] = request_id
        token = current_request_id.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False
        self.metrics.begin_request()

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message.get("status", 500))
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            status_code = 500
            if response_started:
                raise
            # ServerErrorMiddleware sits outside user middleware. Handling the
            # pre-response failure here preserves the generated correlation ID
            # while returning no internal exception details to the caller.
            # ServerErrorMiddleware가 이 미들웨어 바깥이라 여기서 응답을 대신 보내면
            # uvicorn의 기본 트레이스백 로깅이 실행되지 않는다. exc_info로 스택을 직접 남긴다.
            log.error(
                "unhandled request failure exception_type=%s",
                type(exc).__name__,
                exc_info=exc,
            )
            await JSONResponse(
                {"detail": "서버 내부 오류가 발생했습니다."},
                status_code=500,
            )(scope, receive, send_with_request_id)
        finally:
            self.metrics.record_request(
                method=str(scope.get("method", "OTHER")),
                route=_route_template(scope),
                status_code=status_code,
                duration_seconds=time.perf_counter() - started,
            )
            self.metrics.end_request()
            current_request_id.reset(token)


def prometheus_text(registry: TerraMetrics | None = None) -> str:
    """Render metrics for an admin-safe integration endpoint."""
    return (registry or metrics).prometheus_text()


def observe_readiness(
    checks: Mapping[str, bool],
    *,
    free_disk_bytes: int | None = None,
    registry: TerraMetrics | None = None,
) -> None:
    """Integration helper for the existing readiness handler."""
    (registry or metrics).record_readiness(checks=checks, free_disk_bytes=free_disk_bytes)


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    if route is None:
        return "unmatched"
    value = getattr(route, "path_format", None) or getattr(route, "path", None)
    return value if isinstance(value, str) else "other"


def _bounded_nonnegative(value: Any, *, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(maximum, integer))


def _bounded_float(value: Any, *, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(maximum, number))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    return format(value, ".12g")


metrics = TerraMetrics()
