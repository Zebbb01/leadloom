"""The few HubSpot CRM v3/v4 calls LeadLoom needs, on HubSpot's free CRM. Every call
goes through http.send, so rate limits (429) and server errors are retried."""

import re

import httpx

from .http import send

CONTACT_PROPS = ["email", "firstname", "lastname", "lifecyclestage", "hs_lead_status", "company", "phone"]
# HUBSPOT_DEFINED association types (CRM associations v4).
DEAL_TO_CONTACT, DEAL_TO_COMPANY = 3, 5
NOTE_TO_CONTACT, NOTE_TO_COMPANY, NOTE_TO_DEAL = 202, 190, 214
# Object type IDs used in record URLs.
TYPE_IDS = {"contact": "0-1", "company": "0-2", "deal": "0-3"}


def client(token: str, base_url: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(15.0, connect=5.0),
        transport=transport,
    )


def _post(c: httpx.Client, path: str, body: dict) -> dict:
    return send(c, "POST", path, what="HubSpot", json=body).json()


def _search(c: httpx.Client, object_type: str, prop: str, value: str, props: list[str], limit: int) -> list[dict]:
    body = {
        "filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}],
        "properties": props,
        "limit": limit,
    }
    return _post(c, f"/crm/v3/objects/{object_type}/search", body).get("results", [])


def find_contact(c: httpx.Client, email: str) -> dict | None:
    found = _search(c, "contacts", "email", email, CONTACT_PROPS, 1)
    return found[0] if found else None


def contacts_at_domain(c: httpx.Client, domain: str) -> list[dict]:
    return _search(c, "contacts", "hs_email_domain", domain, CONTACT_PROPS, 20)


def upsert_company(c: httpx.Client, domain: str, props: dict) -> tuple[str, bool]:
    """Finds the company by domain or creates it. An existing company keeps its data."""
    found = _search(c, "companies", "domain", domain, ["name", "domain"], 1)
    if found:
        return found[0]["id"], False
    return _post(c, "/crm/v3/objects/companies", {"properties": {"domain": domain, **props}})["id"], True


def upsert_contact(c: httpx.Client, email: str, props: dict) -> str:
    """Creates or updates the contact by email in one idempotent call."""
    body = {"inputs": [{"idProperty": "email", "id": email, "properties": {"email": email, **props}}]}
    return _post(c, "/crm/v3/objects/contacts/batch/upsert", body)["results"][0]["id"]


def associate_default(c: httpx.Client, from_type: str, from_id: str, to_type: str, to_id: str) -> None:
    send(c, "PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}", what="HubSpot")


def _to(record_id: str | None, type_id: int) -> list[dict]:
    if not record_id:
        return []
    return [
        {"to": {"id": record_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}
    ]


def create_deal(c: httpx.Client, props: dict, contact_id: str, company_id: str | None) -> str:
    associations = _to(contact_id, DEAL_TO_CONTACT) + _to(company_id, DEAL_TO_COMPANY)
    return _post(c, "/crm/v3/objects/deals", {"properties": props, "associations": associations})["id"]


def create_note(
    c: httpx.Client, html: str, timestamp_ms: int, contact_id: str, company_id: str | None, deal_id: str | None
) -> str:
    associations = _to(contact_id, NOTE_TO_CONTACT) + _to(company_id, NOTE_TO_COMPANY) + _to(deal_id, NOTE_TO_DEAL)
    body = {"properties": {"hs_note_body": html[:65_000], "hs_timestamp": timestamp_ms}, "associations": associations}
    return _post(c, "/crm/v3/objects/notes", body)["id"]


def app_host(token: str) -> str:
    """Each HubSpot account lives in one data centre, which its key names (pat-na2-...)."""
    m = re.match(r"pat-([a-z]+\d+)-", token)
    region = m.group(1) if m else "na1"
    return "app.hubspot.com" if region == "na1" else f"app-{region}.hubspot.com"


def record_url(portal_id: str, kind: str, record_id: str | None, host: str = "app.hubspot.com") -> str | None:
    if not portal_id or not record_id:
        return None
    return f"https://{host}/contacts/{portal_id}/record/{TYPE_IDS[kind]}/{record_id}"
