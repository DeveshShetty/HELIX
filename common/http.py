"""Service-to-service JSON calls with the shared retry policy.

Rules, the same for every caller:
- network errors and 5xx are retried (common/retry.py: backoff with jitter, max attempts);
- 4xx is an answer, not a failure: returned at once as an HTTPException with the
  upstream's detail;
- when retries run out the caller gets 503 "<name> unavailable".
"""
import httpx
from fastapi import HTTPException

from common import retry


class UpstreamError(Exception):
    """The upstream answered 5xx."""

    def __init__(self, response: httpx.Response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


def _detail(r: httpx.Response) -> str:
    try:
        return r.json().get("detail", r.text)
    except ValueError:
        return r.text[:200]


def request_json(method: str, url: str, body: dict | None = None, *, name: str,
                 attempts: int = 3, timeout: float = 3.0, headers: dict | None = None) -> dict:
    def once() -> httpx.Response:
        r = httpx.request(method, url, json=body, timeout=timeout, headers=headers)
        if r.status_code >= 500:
            raise UpstreamError(r)
        return r

    try:
        r = retry.call(once, attempts=attempts, base=0.3, cap=2.0,
                       retryable=lambda e: isinstance(e, (httpx.TransportError, UpstreamError)),
                       label=name, log=lambda m: print(m, flush=True))
    except (httpx.TransportError, UpstreamError) as exc:
        raise HTTPException(503, f"{name} unavailable: {exc!r}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, _detail(r))
    return r.json()
