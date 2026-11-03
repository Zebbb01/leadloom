import base64
import json

import pytest

from app.intake import Ignored, IntakeError, booking_signature, parse_booking, parse_email, parse_form

WS = "0123456789abcdef0123456789abcdef"


def basic(password: str) -> str:
    return "Basic " + base64.b64encode(f"postmark:{password}".encode()).decode()


def test_form_json_and_honeypot():
    body = json.dumps(
        {
            "submission_id": "s1",
            "email": " Ana@Example.COM ",
            "name": "Ana  Lima",
            "nickname": "bot",
            "message": "Hi",
            "workspace": WS,
        }
    ).encode()
    lead = parse_form(body, "application/json")
    assert (lead.email, lead.name, lead.external_id, lead.workspace, lead.honeypot) == (
        "ana@example.com",
        "Ana Lima",
        "s1",
        WS,
        True,
    )


def test_form_urlencoded_without_submission_id_dedupes_by_content():
    body = b"email=tom%40fleet.example&message=Need+a+demo&name=Tom"
    first, second = (
        parse_form(body, "application/x-www-form-urlencoded"),
        parse_form(body, "application/x-www-form-urlencoded"),
    )
    assert first.email == "tom@fleet.example" and first.message == "Need a demo"
    assert first.external_id == second.external_id and len(first.external_id) == 32
    assert not first.honeypot and first.workspace is None


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "x@y.z1"])
def test_form_rejects_bad_email(email):
    with pytest.raises(IntakeError) as e:
        parse_form(json.dumps({"email": email}).encode(), "application/json")
    assert e.value.status == 422


def test_form_rejects_non_object_json():
    with pytest.raises(IntakeError) as e:
        parse_form(b"[1, 2]", "application/json")
    assert e.value.status == 400


def postmark(**extra) -> bytes:
    return json.dumps(
        {
            "FromFull": {"Email": "Priya@AlpineComfort.example", "Name": "Priya Raman"},
            "Subject": "Pricing?",
            "TextBody": "How much for 18 vans?",
            "MessageID": "m-1",
            **extra,
        }
    ).encode()


def test_email_needs_the_right_password():
    with pytest.raises(IntakeError) as e:
        parse_email(postmark(), basic("wrong"), "mail-secret")
    assert e.value.status == 401
    with pytest.raises(IntakeError) as e:
        parse_email(postmark(), "", "mail-secret")
    assert e.value.status == 401
    with pytest.raises(IntakeError) as e:
        parse_email(postmark(), basic("anything"), "")
    assert e.value.status == 503


def test_email_parses_postmark_and_demo_mailbox_hash():
    lead = parse_email(postmark(MailboxHash=f"demo-{WS}"), basic("mail-secret"), "mail-secret")
    assert lead.email == "priya@alpinecomfort.example"
    assert lead.external_id == "m-1"
    assert lead.message == "Pricing?\n\nHow much for 18 vans?"
    assert lead.workspace == WS
    assert parse_email(postmark(MailboxHash="sales"), basic("mail-secret"), "mail-secret").workspace is None


def cal(event: str = "BOOKING_CREATED", **payload) -> bytes:
    p = {
        "uid": "bk-1",
        "startTime": "2026-11-05T09:30:00Z",
        "attendees": [{"email": "maria@brightpath.example", "name": "Maria Santos"}],
        "responses": {
            "company": {"label": "Company", "value": "BrightPath"},
            "website": "brightpath.example",
            "notes": {"value": "140 vans"},
        },
        "metadata": {"workspace": WS},
        **payload,
    }
    return json.dumps({"triggerEvent": event, "payload": p}).encode()


def test_booking_signature_is_checked():
    body = cal()
    with pytest.raises(IntakeError) as e:
        parse_booking(body, booking_signature(body, "other-secret"), "cal-secret")
    assert e.value.status == 401
    with pytest.raises(IntakeError) as e:
        parse_booking(body, "", "cal-secret")
    assert e.value.status == 401


def test_booking_parses_cal_payload():
    body = cal()
    lead = parse_booking(body, booking_signature(body, "cal-secret").upper(), "cal-secret")
    assert (lead.email, lead.name, lead.company, lead.website, lead.message) == (
        "maria@brightpath.example",
        "Maria Santos",
        "BrightPath",
        "brightpath.example",
        "140 vans",
    )
    assert lead.meeting_at.isoformat() == "2026-11-05T09:30:00+00:00"
    assert lead.external_id == "bk-1" and lead.workspace == WS


def test_booking_ignores_other_events():
    body = cal("BOOKING_CANCELLED")
    with pytest.raises(Ignored):
        parse_booking(body, booking_signature(body, "cal-secret"), "cal-secret")
