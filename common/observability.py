"""Prometheus instrumentation shared by every service (adapted from Lab 3/4).

The `route` label uses the route TEMPLATE (/tickets/{ticket_id}), not the raw
path, so label cardinality stays bounded.
"""
import time

from fastapi import FastAPI, Request
from prometheus_client import Counter, Histogram, make_asgi_app

REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled",
    ["service", "method", "route", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
EVENTS_CONSUMED = Counter(
    "events_consumed_total", "Events handled by consumers",
    ["service", "event_type", "outcome"],  # outcome: processed | duplicate | error
)


def instrument(app: FastAPI, service: str) -> None:
    @app.middleware("http")
    async def _metrics(request: Request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route_obj = request.scope.get("route")
            route = getattr(route_obj, "path", "unmatched")
            REQUESTS.labels(service, request.method, route, str(status)).inc()
            LATENCY.labels(service, request.method, route).observe(time.perf_counter() - started)

    app.mount("/metrics", make_asgi_app())
