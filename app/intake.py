"""Turns the three webhook formats into one lead, after checking that each came from
its sender. n8n forwards the raw body and the headers that carry the proof."""

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Literal
from urllib.parse import parse_qs

from pydantic import BaseModel

from .config import Settings

Source = Literal["form", "email", "booking"]
SOURCES: tuple[Source, ...] = ("form", "email", "booking")
EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,63}$")
WORKSPACE_RE = re.compile(r"^[0-9a-f]{32}$")


class Lead(BaseModel):
    source: Source
    external_id: str
    email: str
    name: str = ""
    company: str = ""
    website: str = ""
    phone: str = ""
    title: str = ""
    message: str = ""
    meeting_at: datetime | None = None
    workspace: str | None = None
    # A hidden form field that people never fill in and bots do.
    honeypot: bool = False


class IntakeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Ignored(Exception):
    """A valid delivery that is not a new lead, such as a cancelled booking."""


def _text(value: object, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit] if not isinstance(value, dict | list) else ""


def _email(value: object) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise IntakeError(422, "The lead has no valid email address.")
    return email


def _workspace(value: object) -> str | None:
    value = str(value or "").strip().lower()
    return value if WORKSPACE_RE.match(value) else None


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body)
    except ValueError:
        raise IntakeError(400, "The body is not JSON.") from None
    if not isinstance(data, dict):
        raise IntakeError(400, "The body is not a JSON object.")
    return data


def parse_form(body: bytes, content_type: str) -> Lead:
    if "application/x-www-form-urlencoded" in content_type:
        data: dict = {k: v[-1] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
    else:
        data = _json(body)
    email = _email(data.get("email"))
    message = str(data.get("message") or "").strip()[:5000]
    # Forms that send no submission ID still dedupe a double click.
    external_id = (
        _text(data.get("submission_id"), 100)
        or hashlib.sha256(f"{email}|{message}|{date.today()}".encode()).hexdigest()[:32]
    )
    return Lead(
        source="form",
        external_id=external_id,
        email=email,
        name=_text(data.get("name")),
        company=_text(data.get("company")),
        website=_text(data.get("website"), 300),
        phone=_text(data.get("phone"), 50),
        title=_text(data.get("title")),
        message=message,
        workspace=_workspace(data.get("workspace")),
        honeypot=bool(_text(data.get("nickname"))),
    )


def parse_email(body: bytes, authorization: str, secret: str) -> Lead:
    """Postmark's inbound JSON. Postmark sends the password from the webhook URL as
    HTTP basic auth; the user name is ignored."""
    if not secret:
        raise IntakeError(503, "Inbound email is not configured (INBOUND_EMAIL_SECRET).")
    password = ""
    if authorization.lower().startswith("basic "):
        try:
            password = base64.b64decode(authorization[6:]).decode().partition(":")[2]
        except ValueError:
            password = ""
    if not hmac.compare_digest(password.encode(), secret.encode()):
        raise IntakeError(401, "The inbound email webhook credentials are wrong.")
    data = _json(body)
    sender = data.get("FromFull") or {}
    email = _email(sender.get("Email") or data.get("From"))
    subject = _text(data.get("Subject"), 300)
    text = str(data.get("StrippedTextReply") or data.get("TextBody") or "").strip()
    message_id = _text(data.get("MessageID"), 200)
    if not message_id:
        raise IntakeError(422, "The email has no MessageID.")
    mailbox = str(data.get("MailboxHash") or "")
    return Lead(
        source="email",
        external_id=message_id,
        email=email,
        name=_text(sender.get("Name") or data.get("FromName")),
        message=f"{subject}\n\n{text}".strip()[:5000],
        workspace=_workspace(mailbox[5:]) if mailbox.startswith("demo-") else None,
    )


def booking_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def parse_booking(body: bytes, signature: str, secret: str) -> Lead:
    """A Cal.com BOOKING_CREATED webhook, signed with X-Cal-Signature-256."""
    if not secret:
        raise IntakeError(503, "Bookings are not configured (CAL_WEBHOOK_SECRET).")
    if not hmac.compare_digest(signature.strip().lower(), booking_signature(body, secret)):
        raise IntakeError(401, "The booking webhook signature does not match.")
    data = _json(body)
    if data.get("triggerEvent") != "BOOKING_CREATED":
        raise Ignored(f"Ignored {data.get('triggerEvent') or 'an event without a trigger'}.")
    p = data.get("payload") or {}
    attendee = (p.get("attendees") or [{}])[0]
    responses = p.get("responses") or {}

    def answer(*keys: str) -> str:
        for k in keys:
            v = responses.get(k)
            v = v.get("value") if isinstance(v, dict) else v
            if v and not isinstance(v, dict | list):
                return str(v)
        return ""

    uid = _text(p.get("uid"), 100)
    if not uid:
        raise IntakeError(422, "The booking has no uid.")
    try:
        meeting_at = datetime.fromisoformat(str(p.get("startTime")).replace("Z", "+00:00"))
    except ValueError:
        meeting_at = None
    return Lead(
        source="booking",
        external_id=uid,
        email=_email(attendee.get("email") or answer("email")),
        name=_text(attendee.get("name") or answer("name")),
        company=_text(answer("company")),
        website=_text(answer("website"), 300),
        phone=_text(answer("attendeePhoneNumber", "phone"), 50),
        title=_text(answer("title", "role")),
        message=str(p.get("additionalNotes") or answer("notes") or p.get("description") or "").strip()[:5000],
        meeting_at=meeting_at.astimezone(UTC) if meeting_at and meeting_at.tzinfo else None,
        workspace=_workspace((p.get("metadata") or {}).get("workspace")),
    )


def parse(source: str, body: bytes, headers: Mapping[str, str], s: Settings) -> Lead:
    if source == "form":
        return parse_form(body, headers.get("content-type", ""))
    if source == "email":
        return parse_email(body, headers.get("x-source-authorization", ""), s.inbound_email_secret)
    if source == "booking":
        return parse_booking(body, headers.get("x-cal-signature-256", ""), s.cal_webhook_secret)
    raise IntakeError(404, f"Unknown source {source!r}.")
