"""Outbound HTTP with retries: exponential backoff, Retry-After honoured, and errors
that say whether trying again can help."""

import time
from email.utils import parsedate_to_datetime

import httpx

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
# A service asking us to wait longer than this has run out of allowance for now.
MAX_WAIT_S = 60.0


class HttpError(Exception):
    def __init__(self, message: str, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def retry_after(r: httpx.Response) -> float:
    value = r.headers.get("retry-after", "")
    if value.replace(".", "", 1).isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return 0.0


def backoff_s(attempt: int, wait: float = 0.0, base: float = 0.5) -> float:
    """0.5 s, 1 s, 2 s, 4 s... capped at 8 s, or longer when the service asks for it."""
    return max(wait, min(8.0, base * 2 ** (attempt - 1)))


def send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    what: str,
    attempts: int = 4,
    **kwargs,
) -> httpx.Response:
    for attempt in range(1, attempts + 1):
        wait = 0.0
        try:
            r = client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            err = HttpError(f"{what} could not be reached ({type(e).__name__}).", retryable=True)
        else:
            if r.status_code < 400:
                return r
            wait = retry_after(r)
            err = HttpError(
                f"{what} returned {r.status_code}: {r.text[:300]}",
                retryable=r.status_code in RETRYABLE and wait <= MAX_WAIT_S,
                status=r.status_code,
            )
        if not err.retryable or attempt == attempts:
            raise err
        time.sleep(backoff_s(attempt, wait))
    raise AssertionError("unreachable")
