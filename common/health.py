"""Readiness checks shared by every service's /health.

/health used to return 200 as soon as the web server was up, even if the
service's database was unusable or its event consumer was disconnected. Now it
checks what the service needs and answers 503 with the failing check named, so
Docker's HEALTHCHECK, `depends_on: service_healthy` and the demo scripts see the
real state.
"""
from fastapi.responses import JSONResponse

from common import messaging
from common.db import connect


def db_ok(db_path: str) -> tuple[bool, str]:
    try:
        conn = connect(db_path)
        try:
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
        return True, "ok"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


def report(service: str, db_path: str | None = None, consumes: tuple[str, ...] = (),
           extra: dict | None = None, ready: bool = True, not_ready_reason: str = "") -> JSONResponse:
    """200 {"status":"ok"} when every check passes, else 503 {"status":"unavailable"|"starting"}."""
    checks: dict[str, str] = {}
    if not ready:
        checks["startup"] = not_ready_reason or "starting"
    if db_path:
        ok, detail = db_ok(db_path)
        checks["database"] = detail
    for queue in consumes:
        checks[f"broker:{queue}"] = "consuming" if messaging.CONSUMING.get(queue) else "disconnected"
    failing = [k for k, v in checks.items() if v not in ("ok", "consuming")]
    status = "ok" if not failing else ("starting" if not ready else "unavailable")
    body = {"status": status, "service": service, "checks": checks, **(extra or {})}
    if failing:
        body["failing"] = failing
    return JSONResponse(body, status_code=200 if not failing else 503)
