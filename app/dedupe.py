"""Finds the contact a lead already is (same email), and people who are probably the
same contact under another address (same company domain and name, or same phone)."""

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlsplit

import httpx

from . import hubspot
from .db import Pool
from .intake import Lead

FREE_EMAIL_DOMAINS = frozenset(
    {"gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com", "msn.com"}
    | {"icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com"}
    | {"yandex.com", "zoho.com", "hey.com", "fastmail.com"}
)
DISPOSABLE_DOMAINS = frozenset(
    {"mailinator.com", "guerrillamail.com", "10minutemail.com", "temp-mail.org", "tempmail.com", "yopmail.com"}
    | {"trashmail.com", "sharklasers.com", "dispostable.com", "maildrop.cc", "getnada.com"}
)


def email_domain(email: str) -> str:
    return email.rpartition("@")[2].lower()


def site_domain(website: str) -> str | None:
    website = website.strip()
    if not website:
        return None
    host = urlsplit(website if "://" in website else f"https://{website}").hostname or ""
    host = host.lower().removeprefix("www.")
    return host if "." in host else None


def company_domain(lead: Lead) -> str | None:
    """The company's domain: a work email's domain, else the website's."""
    domain = email_domain(lead.email)
    return site_domain(lead.website) if domain in FREE_EMAIL_DOMAINS else domain


def _norm(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z ]", " ", ascii_name.lower()).split())


def same_person(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if SequenceMatcher(None, a, b).ratio() >= 0.85:
        return True
    # "J. Rivera" and "Joan Rivera": same last name, same first initial.
    wa, wb = a.split(), b.split()
    return len(wa) > 1 and len(wb) > 1 and wa[-1] == wb[-1] and wa[0][0] == wb[0][0]


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)[-9:]


def find(db: Pool, lead_id: str, lead: Lead, crm: httpx.Client | None) -> dict:
    domain = email_domain(lead.email)
    work_domain = None if domain in FREE_EMAIL_DOMAINS else domain
    result: dict = {"existing": None, "possible_duplicates": [], "open_deal_id": None}

    # ponytail: compares against the 200 newest matching leads in Python; move the
    # name match into SQL (pg_trgm) if one workspace ever holds many thousands.
    with db.connection() as c:
        earlier = c.execute(
            """select id, lead, hubspot, status from leads
               where id <> %s and workspace is not distinct from %s
                 and (email = %s or split_part(email, '@', 2) = %s or lead->>'phone' <> '')
               order by created_at desc limit 200""",
            (lead_id, lead.workspace, lead.email, work_domain or "-"),
        ).fetchall()

    same_email = [r for r in earlier if r["lead"]["email"] == lead.email]
    if same_email:
        synced = next((r for r in same_email if r["hubspot"]), None)
        result["existing"] = {
            "earlier_leads": len(same_email),
            "hubspot_id": synced["hubspot"].get("contact_id") if synced else None,
        }
        deal = next(
            (r["hubspot"].get("deal_id") for r in same_email if r["hubspot"] and r["hubspot"].get("deal_id")), None
        )
        result["open_deal_id"] = deal

    seen = {lead.email}
    for r in earlier:
        other = r["lead"]
        if other["email"] in seen:
            continue
        reason = None
        if (
            work_domain
            and email_domain(other["email"]) == work_domain
            and same_person(lead.name, other.get("name", ""))
        ):
            reason = f"same name at {work_domain}"
        elif lead.phone and _digits(lead.phone) and _digits(lead.phone) == _digits(other.get("phone", "")):
            reason = "same phone number"
        if reason:
            seen.add(other["email"])
            result["possible_duplicates"].append(
                {"email": other["email"], "name": other.get("name", ""), "reason": reason, "where": "LeadLoom"}
            )

    if crm is not None:
        contact = hubspot.find_contact(crm, lead.email)
        if contact:
            p = contact.get("properties", {})
            result["existing"] = {
                **(result["existing"] or {"earlier_leads": 0}),
                "hubspot_id": contact["id"],
                "lifecyclestage": p.get("lifecyclestage"),
                "lead_status": p.get("hs_lead_status"),
            }
        if work_domain:
            for other in hubspot.contacts_at_domain(crm, work_domain):
                p = other.get("properties", {})
                email = (p.get("email") or "").lower()
                name = f"{p.get('firstname') or ''} {p.get('lastname') or ''}".strip()
                if email and email not in seen and same_person(lead.name, name):
                    seen.add(email)
                    result["possible_duplicates"].append(
                        {"email": email, "name": name, "reason": f"same name at {work_domain}", "where": "HubSpot"}
                    )
    return result
