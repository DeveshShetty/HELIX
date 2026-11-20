"""Structured logging with a correlation id on every line, set up once for all services.

One id follows one piece of work across services:
  HTTP    X-Request-Id header: read on the way in (or a new one is made), returned on
          the response, and sent on every outbound call (common/http.py, chat/clients.py)
  broker  the event's correlation_id (AMQP property + envelope field): new_event() stamps
          the current id, consume_forever() restores it before the handler runs

Line format (key=value, greppable):
  2026-11-21T10:15:02.120Z INFO  service=chat corr=req-3f9a1c2e chat | escalated d-1234 -> T-000051
"""
import contextvars
import logging
import os
import sys
import time
import uuid

CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)
HEADER = "X-Request-Id"
_service = "-"


def current() -> str | None:
    return CORRELATION_ID.get()


def new_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def set_id(value: str | None) -> contextvars.Token:
    return CORRELATION_ID.set(value)


def headers() -> dict:
    """Headers for an outbound HTTP call: carries the current correlation id."""
    cid = current()
    return {HEADER: cid} if cid else {}


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.service = _service
        record.corr = CORRELATION_ID.get() or "-"
        return True


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):              # noqa: N802 (logging API name)
        return time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created)) + f".{int(record.msecs):03d}Z"


def setup(service: str) -> None:
    """Configure the root logger once per process. Safe to call more than once."""
    global _service
    _service = service
    root = logging.getLogger()
    if any(getattr(h, "_helix", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler._helix = True                                     # type: ignore[attr-defined]
    handler.addFilter(_ContextFilter())
    handler.setFormatter(_UTCFormatter("%(asctime)s %(levelname)-5s service=%(service)s corr=%(corr)s "
                                       "%(name)s | %(message)s"))
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    for noisy in ("pika", "httpx", "httpcore"):               # their INFO lines drown ours
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def install(app, service: str) -> None:
    """FastAPI middleware: take X-Request-Id from the request (or make one), keep it in
    the context for the whole request, and return it on the response."""
    setup(service)

    @app.middleware("http")
    async def _correlation(request, call_next):
        cid = request.headers.get(HEADER) or new_id()
        token = CORRELATION_ID.set(cid)
        try:
            response = await call_next(request)
        finally:
            CORRELATION_ID.reset(token)
        response.headers[HEADER] = cid
        return response
