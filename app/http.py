"""Outbound HTTP with retries: exponential backoff, Retry-After honoured, and errors
that say whether trying again can help."""

import logging
import time
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger(__name__)

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
# A service asking us to wait longer than this has run out of allowance for now.
MAX_WAIT_S = 60.0


class HttpError(Exception):
    """str() is a sentence a person can read on a page; detail is what the service
    itself said, for the logs and the audit trail."""

    def __init__(self, message: str, retryable: bool, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.detail = detail


def plain(what: str, status: int) -> str:
    if status == 429:
        return f"{what} was over its rate limit or free quota (HTTP 429)"
    if status in (401, 403):
        return f"{what} did not accept our credentials (HTTP {status})"
    if status >= 500:
        return f"{what} was down or overloaded (HTTP {status})"
    return f"{what} refused the request (HTTP {status})"


def said(r: httpx.Response) -> str:
    """The service's own error message: the message of a JSON error (the OpenAI, Gemini
    and HubSpot shapes), short plain text as it is, and nothing from an HTML page."""
    try:
        body = r.json()
    except ValueError:
        text = r.text.strip()
        return "" if text.startswith("<") else text[:200]
    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    message = error.get("message") if isinstance(error, dict) else body.get("message") or error
    return message[:300] if isinstance(message, str) else ""


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
            err = HttpError(f"{what} could not be reached", retryable=True, detail=type(e).__name__)
        else:
            if r.status_code < 400:
                return r
            wait = retry_after(r)
            err = HttpError(
                plain(what, r.status_code),
                retryable=r.status_code in RETRYABLE and wait <= MAX_WAIT_S,
                status=r.status_code,
                detail=said(r),
            )
        if not err.retryable or attempt == attempts:
            log.warning("%s after %d attempt(s): %s", err, attempt, err.detail or "no detail")
            raise err
        time.sleep(backoff_s(attempt, wait))
    raise AssertionError("unreachable")
