import base64
import json
from urllib.parse import unquote_plus
from urllib.robotparser import RobotFileParser

import pytest
from fastapi.testclient import TestClient

from app.intake import booking_signature, parse_booking, parse_email, parse_form
from app.main import ROBOTS, create_app
from tests.conftest import make_settings

KEY = {"Authorization": "Bearer test-key"}


@pytest.fixture
def make_client(settings, db, router):
    clients = []

    def make(s=settings) -> TestClient:
        client = TestClient(create_app(s, transport=router), follow_redirects=False)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def booking(event="BOOKING_CREATED", uid="bk-1") -> bytes:
    return json.dumps(
        {
            "triggerEvent": event,
            "payload": {
                "uid": uid,
                "startTime": "2026-11-05T09:30:00Z",
                "additionalNotes": "Interested. Call me.",
                "attendees": [{"email": "chris@doyle-freight.example", "name": "Chris Doyle"}],
            },
        }
    ).encode()


def signed(body: bytes, secret="cal-secret") -> dict:
    return {**KEY, "X-Cal-Signature-256": booking_signature(body, secret), "Content-Type": "application/json"}


def test_api_needs_the_key(client):
    assert client.post("/v1/intake/form", content=b"{}").status_code == 401
    assert client.post("/v1/intake/form", content=b"{}", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/v1/intake/fax", content=b"{}", headers=KEY).status_code == 404


def test_intake_is_idempotent_and_checks_the_sender(client):
    body = booking()
    first = client.post("/v1/intake/booking", content=body, headers=signed(body))
    again = client.post("/v1/intake/booking", content=body, headers=signed(body))
    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == {"lead_id": first.json()["lead_id"], "duplicate": True}
    assert client.post("/v1/intake/booking", content=body, headers=signed(body, "forged")).status_code == 401
    cancelled = booking("BOOKING_CANCELLED")
    assert client.post("/v1/intake/booking", content=cancelled, headers=signed(cancelled)).status_code == 202


def test_review_flow_over_http(client, router):
    body = booking()
    lead_id = client.post("/v1/intake/booking", content=body, headers=signed(body)).json()["lead_id"]
    q = client.post(f"/v1/leads/{lead_id}/qualify", headers=KEY).json()
    assert q["next"] == "review" and "confidence" in q["decision"]["review_reasons"][0]
    resume = "http://n8n.test/webhook-waiting/9?signature=s"
    assert client.post(f"/v1/leads/{lead_id}/hold", json={"resume_url": resume}, headers=KEY).status_code == 200

    # A live lead is hidden from visitors until a reviewer signs in.
    assert client.get(f"/leads/{lead_id}").status_code == 404
    assert "not right" in client.post("/signin", data={"key": "guess"}).text
    assert client.post("/signin", data={"key": "test-reviewer-key"}).status_code == 303
    page = client.get(f"/leads/{lead_id}")
    assert page.status_code == 200 and "Needs a person" in page.text

    r = client.post(
        f"/leads/{lead_id}/review",
        data={"action": "approve", "route": "sales", "score": "81", "note": "Booked a call."},
    )
    assert r.status_code == 303 and "picked the lead up" in unquote_plus(r.headers["location"])
    sent = router.to("n8n.test", "/webhook-waiting/9")
    assert json.loads(sent[0].content) == {"lead_id": lead_id, "decision": "approve"}

    synced = client.post(f"/v1/leads/{lead_id}/sync", headers=KEY).json()
    assert synced["status"] == "synced" and "Hot lead" in synced["alert"]["text"]
    assert client.post(
        f"/v1/leads/{lead_id}/events", json={"action": "alert_sent", "detail": {"channel": "slack"}}, headers=KEY
    ).json() == {"ok": True}
    page = client.get(f"/leads/{lead_id}").text
    assert "Synced" in page and synced["hubspot"]["contact_id"] in page and "alert sent" in page
    assert client.post(f"/v1/leads/{lead_id}/sync", headers=KEY).json()["alert"] is None


def test_demo_leads_stay_in_their_browser(client, make_client):
    client.get("/")
    ws = client.cookies["ll_ws"]
    body = json.dumps({"email": "ana@fleet.example", "message": "Demo please", "workspace": ws}).encode()
    lead_id = client.post("/v1/intake/form", content=body, headers=KEY).json()["lead_id"]
    assert client.get(f"/leads/{lead_id}").status_code == 200
    assert "ana@fleet.example" in client.get("/leads").text
    stranger = make_client()
    assert stranger.get(f"/leads/{lead_id}").status_code == 404
    assert "ana@fleet.example" not in stranger.get("/leads").text
    stranger.post("/signin", data={"key": "test-reviewer-key"})
    assert stranger.get(f"/leads/{lead_id}").status_code == 200


@pytest.mark.parametrize(
    "sample,channel",
    [
        ("brightpath-booking", ""),
        ("alpine-hvac-question", ""),
        ("northwind-pricing", ""),
        ("northwind-pricing", "booking"),
    ],
)
def test_demo_sends_what_the_real_sender_would(client, router, sample, channel):
    client.get("/")
    ws = client.cookies["ll_ws"]
    router.replies["n8n.test"] = (200, {"lead_id": "11111111-1111-1111-1111-111111111111", "duplicate": False})
    r = client.post("/demo/send", data={"sample": sample, "channel": channel, "message": "Edited message"})
    assert r.headers["location"] == "/leads/11111111-1111-1111-1111-111111111111"
    sent = router.to("n8n.test")[-1]
    source = sent.url.path.rsplit("/", 1)[1]
    if source == "booking":
        lead = parse_booking(sent.content, sent.headers["x-cal-signature-256"], "cal-secret")
    elif source == "email":
        lead = parse_email(sent.content, sent.headers["authorization"], "mail-secret")
    else:
        lead = parse_form(sent.content, sent.headers["content-type"])
    assert lead.workspace == ws and lead.message.endswith("Edited message")
    # Forms and bookings carry a website field; an email has only its sender's domain.
    assert lead.website.startswith("http://testserver/sample-sites/") or source == "email"


def test_demo_limits(make_client, settings, router):
    client = make_client(make_settings(settings.database_url, demo_daily_per_workspace=0))
    r = client.post("/demo/send", data={"sample": "brightpath-booking"})
    assert "tomorrow" in r.headers["location"] and router.to("n8n.test") == []
    assert client.post("/demo/send", data={"sample": "customer-support"}).status_code == 404


def test_sample_sites_and_robots(client):
    assert "BrightPath Logistics" in client.get("/sample-sites/brightpath-logistics").text
    assert client.get("/sample-sites/..%2Fpyproject").status_code == 404
    robots = RobotFileParser()
    robots.parse(client.get("/robots.txt").text.splitlines())
    assert robots.can_fetch("LeadLoomBot", "/sample-sites/brightpath-logistics")
    assert not robots.can_fetch("LeadLoomBot", "/sample-sites/quiet-harbor-movers")
    assert not robots.can_fetch("Googlebot", "/")
    assert ROBOTS.startswith("User-agent: LeadLoomBot")


def test_pages_render(client):
    home = client.get("/")
    assert home.status_code == 200 and "Send through n8n" in home.text
    assert "No leads yet" in client.get("/leads").text
    assert client.get("/health").json() == {"ok": True}


def test_basic_auth_helper_matches_postmark():
    header = "Basic " + base64.b64encode(b"postmark:mail-secret").decode()
    body = json.dumps({"FromFull": {"Email": "a@b.example"}, "MessageID": "x"}).encode()
    assert parse_email(body, header, "mail-secret").email == "a@b.example"
