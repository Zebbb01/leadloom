"""A lead's life: stored once per delivery, qualified once (dedupe, enrich, score),
held for a person when needed, then pushed to HubSpot. Model and HubSpot calls are
durable steps, so a retry resumes where the last attempt stopped. Every change goes
into the append-only audit log."""

import html
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from . import dedupe, enrich, hubspot, llm, scoring
from .config import Settings
from .db import Pool
from .http import HttpError, send
from .intake import Lead

DECIDED_NEXT = {
    "awaiting_review": "review",
    "approved": "sync",
    "failed": "sync",
    "synced": "done",
    "rejected": "discard",
    "discarded": "discard",
}


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def audit(c: Connection[DictRow], lead_id: str, actor: str, action: str, detail: dict | None = None) -> None:
    c.execute(
        "insert into audit (lead_id, actor, action, detail) values (%s, %s, %s, %s)",
        (lead_id, actor, action, Jsonb(detail or {})),
    )


def get(db: Pool, lead_id: str) -> dict:
    with db.connection() as c:
        row = c.execute("select * from leads where id = %s", (lead_id,)).fetchone()
    if not row:
        raise NotFound(lead_id)
    return row


def next_step(row: dict) -> str:
    if row["status"] == "qualified":
        return row["decision"]["next"]
    return DECIDED_NEXT.get(row["status"], "qualify")


@contextmanager
def lead_lock(db: Pool, lead_id: str) -> Iterator[None]:
    """One qualify or sync per lead at a time. An overlapping call (n8n retrying while
    the first call still runs) gets a 409, and n8n tries again a few seconds later."""
    with db.connection() as c:
        row = c.execute("select pg_try_advisory_lock(hashtext(%s)) as ok", (lead_id,)).fetchone()
        if not row or not row["ok"]:
            raise Conflict("This lead is being processed; try again shortly.")
        try:
            yield
        finally:
            c.execute("select pg_advisory_unlock(hashtext(%s))", (lead_id,))


def step(db: Pool, lead_id: str, name: str, fn: Callable[[], tuple[Any, dict | None]], model: str = "") -> Any:
    """Runs fn once per lead: a finished step is read back instead of run again."""
    with db.connection() as c:
        row = c.execute("select output from steps where lead_id = %s and name = %s", (lead_id, name)).fetchone()
    if row:
        return row["output"]
    started = time.perf_counter()
    output, usage = fn()
    tokens_in, tokens_out = (usage or {}).get("tokens_in", 0), (usage or {}).get("tokens_out", 0)
    with db.connection() as c:
        c.execute(
            """insert into steps (lead_id, name, output, tokens_in, tokens_out, cost_usd, ms)
               values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing""",
            (
                lead_id,
                name,
                Jsonb(output),
                tokens_in,
                tokens_out,
                llm.cost_usd(model, tokens_in, tokens_out),
                round((time.perf_counter() - started) * 1000),
            ),
        )
    return output


def store(db: Pool, lead: Lead) -> tuple[str, bool]:
    """Saves the lead; a redelivered webhook returns the lead it already made."""
    with db.connection() as c, c.transaction():
        row = c.execute(
            """insert into leads (source, external_id, workspace, email, lead) values (%s, %s, %s, %s, %s)
               on conflict (source, external_id) do nothing returning id""",
            (lead.source, lead.external_id, lead.workspace, lead.email, Jsonb(lead.model_dump(mode="json"))),
        ).fetchone()
        created = row is not None
        if not row:
            row = c.execute(
                "select id from leads where source = %s and external_id = %s", (lead.source, lead.external_id)
            ).fetchone()
        assert row, "the conflicting lead exists"
        lead_id = str(row["id"])
        audit(c, lead_id, "n8n", "received" if created else "redelivered", {"source": lead.source})
    return lead_id, created


def enrich_site(db: Pool, s: Settings, web: httpx.Client, url: str) -> dict:
    with db.connection() as c:
        row = c.execute(
            "select data from sites where url = %s and fetched_at > now() - interval '7 days'", (url,)
        ).fetchone()
    if row:
        return {**row["data"], "cached": True}
    data = enrich.enrich(url, web, s.enrich_allow_private)
    if data["status"] in ("ok", "robots_disallowed"):
        with db.connection() as c:
            c.execute(
                """insert into sites (url, data) values (%s, %s)
                   on conflict (url) do update set data = excluded.data, fetched_at = now()""",
                (url, Jsonb(data)),
            )
    return data


def score(
    db: Pool, s: Settings, client: httpx.Client, lead: Lead, dd: dict, site: dict | None
) -> tuple[dict, dict | None]:
    if scoring.junk_rule(lead):
        return {"assessment": None, "error": None}, None
    if not s.llm_api_key:
        return {"assessment": None, "error": "No model is configured (LLM_API_KEY is empty)"}, None
    with db.connection() as c:
        row = c.execute(
            "select count(*) as n from steps where name = 'score' and tokens_in > 0 "
            "and created_at > now() - interval '1 day'"
        ).fetchone()
    used = row["n"] if row else 0
    if used >= s.daily_llm_cap:
        return {"assessment": None, "error": f"Today's cap of {s.daily_llm_cap} model calls is used up"}, None
    try:
        a, usage = llm.complete(
            client, s, scoring.SYSTEM_PROMPT, scoring.user_prompt(lead, dd, site), scoring.Assessment
        )
    except llm.LlmError as e:
        return {"assessment": None, "error": str(e), "detail": e.detail}, e.usage
    return {"assessment": a.model_dump(), "error": None}, usage


def qualify(
    db: Pool, s: Settings, lead_id: str, *, web: httpx.Client, model: httpx.Client, crm: httpx.Client | None
) -> dict:
    """Dedupe, enrich, score and decide the next step. Calling it again returns the
    next step for the lead's current state, so n8n can re-enter at any point."""
    with lead_lock(db, lead_id):
        row = get(db, lead_id)
        if row["status"] != "received":
            return {"lead_id": lead_id, "next": next_step(row), "decision": row["decision"]}
        lead = Lead.model_validate(row["lead"])
        dd: dict = step(db, lead_id, "dedupe", lambda: (dedupe.find(db, lead_id, lead, crm), None))
        domain = dedupe.company_domain(lead)
        url = lead.website or (f"https://{domain}" if domain else "")
        site = step(db, lead_id, "enrich", lambda: (enrich_site(db, s, web, url), None)) if url else None
        scored: dict = step(db, lead_id, "score", lambda: score(db, s, model, lead, dd, site), s.llm_model)
        assessment = scoring.Assessment.model_validate(scored["assessment"]) if scored["assessment"] else None
        decision = {**scoring.decide(lead, assessment, dd, scored["error"]), "dedupe": dd, "site": site}
        with db.connection() as c, c.transaction():
            updated = c.execute(
                "update leads set status = %s, decision = %s, updated_at = now() where id = %s and status = 'received'",
                ("discarded" if decision["next"] == "discard" else "qualified", Jsonb(decision), lead_id),
            ).rowcount
            if updated:
                audit(
                    c,
                    lead_id,
                    "leadloom",
                    "qualified",
                    {
                        "score": decision["score"],
                        "tier": decision["tier"],
                        "route": decision["route"],
                        "next": decision["next"],
                        "rules": [o["rule"] for o in decision["overrides"]],
                        "review_reasons": decision["review_reasons"],
                        "model_error": scored["error"],
                        **({"model_error_detail": scored["detail"]} if scored.get("detail") else {}),
                    },
                )
        row = get(db, lead_id)
        return {"lead_id": lead_id, "next": next_step(row), "decision": row["decision"]}


def hold(db: Pool, s: Settings, lead_id: str, resume_url: str) -> dict:
    """Parks the lead for a person, keeping the URL that resumes n8n's waiting run."""
    parts = urlsplit(resume_url)
    n8n = urlsplit(s.n8n_webhook_url)
    if parts.scheme not in ("http", "https") or (s.n8n_webhook_url and parts.netloc != n8n.netloc):
        raise Conflict("The resume URL must point at this deployment's n8n.")
    with db.connection() as c, c.transaction():
        row = c.execute("select status, decision from leads where id = %s for update", (lead_id,)).fetchone()
        if not row:
            raise NotFound(lead_id)
        if row["status"] == "qualified" and row["decision"]["next"] == "review":
            audit(c, lead_id, "n8n", "awaiting_review", {"reasons": row["decision"]["review_reasons"]})
        elif row["status"] != "awaiting_review":
            raise Conflict(f"The lead is {row['status']}; it is not waiting for review.")
        c.execute(
            "update leads set status = 'awaiting_review', resume_url = %s, updated_at = now() where id = %s",
            (resume_url, lead_id),
        )
    return {"lead_id": lead_id, "status": "awaiting_review"}


def review(
    db: Pool,
    lead_id: str,
    actor: str,
    approve: bool,
    route: str | None = None,
    score: int | None = None,
    note: str = "",
) -> dict:
    """A person's decision. Approval may change the route or the score; the rules then
    work out the tier, the next step and the alert from the edited values."""
    with db.connection() as c, c.transaction():
        row = c.execute(
            "select status, decision, resume_url from leads where id = %s for update", (lead_id,)
        ).fetchone()
        if not row:
            raise NotFound(lead_id)
        if row["status"] != "awaiting_review":
            raise Conflict(f"The lead is {row['status']}; it is not waiting for review.")
        d = dict(row["decision"])
        changes = {}
        if approve and route and route != d["route"]:
            changes["route"] = [d["route"], route]
            d["route"] = route
        if approve and score is not None and score != d["score"]:
            changes["score"] = [d["score"], score]
            d["score_override"] = score
        d["reviewed"] = {
            "by": actor,
            "at": datetime.now(UTC).isoformat(),
            "approved": approve,
            "note": note,
            "changes": changes,
            "reasons": d["review_reasons"],
        }
        d["review_reasons"] = []
        scoring.finish(d)
        status = "rejected" if not approve else "discarded" if d["next"] == "discard" else "approved"
        c.execute(
            "update leads set status = %s, decision = %s, updated_at = now() where id = %s", (status, Jsonb(d), lead_id)
        )
        audit(c, lead_id, actor, "approved" if approve else "rejected", {"changes": changes, "note": note})
    return {"lead_id": lead_id, "status": status, "resume_url": row["resume_url"]}


def note_html(s: Settings, lead_id: str, lead: Lead, d: dict) -> str:
    e = html.escape
    parts = [
        f"<p><strong>LeadLoom: {e(d['tier'])} lead, score {d['score']}/100</strong>, route {e(d['route'])} "
        f"(fit {d['fit']}, intent {d['intent']}, model confidence {d['confidence']:.2f})</p>",
    ]
    if d.get("summary"):
        parts.append(f"<p>{e(d['summary'])}</p>")
    parts.append("<ul>" + "".join(f"<li>{e(r)}</li>" for r in d.get("reasons", [])) + "</ul>")
    if d["overrides"]:
        parts.append(
            "<p><strong>Rules applied</strong></p><ul>"
            + "".join(f"<li>{e(o['rule'])}: {e(o['effect'])}</li>" for o in d["overrides"])
            + "</ul>"
        )
    if r := d.get("reviewed"):
        changes = ", ".join(f"{k} {a} to {b}" for k, (a, b) in r["changes"].items()) or "no changes"
        parts.append(f"<p>Reviewed by {e(r['by'])} ({e(changes)}). {e(r['note'])}</p>")
    site = d.get("site") or {}
    if site.get("status") == "ok":
        parts.append(f"<p>Website: {e(site.get('title', ''))}. {e(site.get('description', ''))}</p>")
    source = {"form": "website form", "email": "inbound email", "booking": "booked meeting"}[lead.source]
    when = f", meeting {lead.meeting_at:%Y-%m-%d %H:%M} UTC" if lead.meeting_at else ""
    parts.append(f'<p>Source: {source}{when}. <a href="{e(s.public_url)}/leads/{lead_id}">Open in LeadLoom</a></p>')
    return "".join(parts)


def push(db: Pool, s: Settings, lead_id: str, lead: Lead, d: dict, crm: httpx.Client) -> dict:
    """Company, contact, deal and note in HubSpot, each a durable step."""
    domain = dedupe.company_domain(lead)
    existing = (d.get("dedupe") or {}).get("existing") or {}
    first, _, last = lead.name.partition(" ")
    company_id = None
    if domain:
        company_id = step(
            db,
            lead_id,
            "hubspot.company",
            lambda: (hubspot.upsert_company(crm, domain, {"name": lead.company or domain})[0], None),
        )
    props = {
        "firstname": first,
        "lastname": last,
        "company": lead.company,
        "website": lead.website,
        "phone": lead.phone,
        "jobtitle": lead.title,
    }
    props = {k: v for k, v in props.items() if v}
    if d["route"] in ("sales", "nurture", "partnerships"):
        props["hs_lead_status"] = "NEW"
    if not existing.get("hubspot_id"):
        props["lifecyclestage"] = {"hot": "salesqualifiedlead", "warm": "marketingqualifiedlead"}.get(d["tier"], "lead")

    def contact() -> tuple[Any, None]:
        contact_id = hubspot.upsert_contact(crm, lead.email, props)
        if company_id:
            hubspot.associate_default(crm, "contacts", contact_id, "companies", str(company_id))
        return contact_id, None

    contact_id = str(step(db, lead_id, "hubspot.contact", contact))

    deal: dict = {}
    if d["route"] == "sales":

        def make_deal() -> tuple[Any, None]:
            open_id = (d.get("dedupe") or {}).get("open_deal_id")
            if open_id:
                try:
                    r = send(
                        crm,
                        "GET",
                        f"/crm/v3/objects/deals/{open_id}",
                        what="HubSpot",
                        params={"properties": "dealstage"},
                    )
                    if r.json()["properties"].get("dealstage") not in ("closedwon", "closedlost"):
                        return {"id": open_id, "reused": True}, None
                except HttpError as e:
                    if e.status != 404:
                        raise
            stage = "qualifiedtobuy" if d["tier"] == "hot" and lead.source != "booking" else "appointmentscheduled"
            what = "demo booked" if lead.source == "booking" else "inbound lead"
            name = f"{lead.company or domain or lead.name}: {what}"
            deal_id = hubspot.create_deal(
                crm,
                {"dealname": name, "pipeline": "default", "dealstage": stage},
                contact_id,
                str(company_id) if company_id else None,
            )
            return {"id": deal_id, "reused": False}, None

        deal = step(db, lead_id, "hubspot.deal", make_deal)
    note_id = step(
        db,
        lead_id,
        "hubspot.note",
        lambda: (
            hubspot.create_note(
                crm,
                note_html(s, lead_id, lead, d),
                int(time.time() * 1000),
                contact_id,
                str(company_id) if company_id else None,
                deal.get("id"),
            ),
            None,
        ),
    )
    urls = {
        kind: hubspot.record_url(s.hubspot_portal_id, kind, rid, hubspot.app_host(s.hubspot_token))
        for kind, rid in (("contact", contact_id), ("company", company_id), ("deal", deal.get("id")))
    }
    return {
        "company_id": company_id,
        "contact_id": contact_id,
        "deal_id": deal.get("id"),
        "deal_reused": deal.get("reused", False),
        "note_id": note_id,
        "urls": urls,
    }


def slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def alert_text(s: Settings, lead_id: str, lead: Lead, d: dict, result: dict) -> str:
    who = f"*{slack_escape(lead.name or lead.email)}*" + (f" ({slack_escape(lead.company)})" if lead.company else "")
    source = {"form": "website form", "email": "email", "booking": "booked a meeting"}[lead.source]
    lines = [f"{'[demo] ' if lead.workspace else ''}:fire: Hot lead: {who}, score {d['score']}/100, via {source}"]
    if d.get("summary"):
        lines.append(f"> {slack_escape(d['summary'])}")
    if lead.meeting_at:
        lines.append(f"Meeting: {lead.meeting_at:%a %d %b %H:%M} UTC")
    links = [f"<{s.public_url}/leads/{lead_id}|Open in LeadLoom>"]
    if url := (result.get("urls") or {}).get("contact"):
        links.append(f"<{url}|HubSpot contact>")
    lines.append(" · ".join(links))
    return "\n".join(lines)


def sync(db: Pool, s: Settings, lead_id: str, crm: httpx.Client | None) -> dict:
    """Pushes the lead to HubSpot. The alert is claimed once, so a retried call never
    alerts twice."""
    with lead_lock(db, lead_id):
        row = get(db, lead_id)
        if row["status"] == "synced":
            return {"lead_id": lead_id, "status": "synced", "hubspot": row["hubspot"], "alert": None}
        if next_step(row) != "sync":
            raise Conflict(f"The lead is {row['status']}; there is nothing to sync.")
        lead, d = Lead.model_validate(row["lead"]), row["decision"]
        try:
            result = push(db, s, lead_id, lead, d, crm) if crm else {"skipped": "HubSpot is not configured."}
        except HttpError as e:
            with db.connection() as c, c.transaction():
                c.execute(
                    "update leads set status = 'failed', error = %s, updated_at = now() where id = %s",
                    (str(e), lead_id),
                )
                audit(
                    c,
                    lead_id,
                    "leadloom",
                    "sync_failed",
                    {"error": str(e), **({"detail": e.detail} if e.detail else {})},
                )
            raise
        with db.connection() as c, c.transaction():
            claimed = (
                d["alert"]
                and c.execute(
                    "update leads set alert_claimed_at = now() where id = %s and alert_claimed_at is null", (lead_id,)
                ).rowcount
                == 1
            )
            c.execute(
                "update leads set status = 'synced', hubspot = %s, error = null, updated_at = now() where id = %s",
                (Jsonb(result), lead_id),
            )
            audit(c, lead_id, "leadloom", "synced", result)
        alert = {"text": alert_text(s, lead_id, lead, d, result)} if claimed else None
        return {"lead_id": lead_id, "status": "synced", "hubspot": result, "alert": alert}


def wait_for_n8n(client: httpx.Client, s: Settings, within_s: float = 150.0) -> None:
    """n8n scales to zero. A waking replica accepts requests before it has set up its
    webhooks and answers them with 404 until /healthz/readiness says 200, so callers
    wait for that first (up to two and a half minutes, or 75 checks)."""
    url = s.n8n_webhook_url.removesuffix("/webhook") + "/healthz/readiness"
    deadline = time.monotonic() + within_s
    for _ in range(75):
        try:
            if client.get(url, timeout=15.0).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            break
        time.sleep(2)
    raise HttpError(f"n8n did not wake up within {within_s:.0f} s", retryable=True)


def resume_n8n(client: httpx.Client, s: Settings, lead_id: str, resume_url: str | None, approved: bool) -> str:
    """Resumes n8n's waiting run with the decision. When that run is gone (a restart
    lost it, or it timed out), an approved lead goes through n8n's retry webhook."""
    body = {"lead_id": lead_id, "decision": "approve" if approved else "reject"}
    if resume_url:
        wait_for_n8n(client, s)
        try:
            send(client, "POST", resume_url, what="n8n", json=body, attempts=3)
            return "resumed"
        except HttpError as e:
            if e.status not in (404, 409, 410):
                raise
    if not approved:
        return "nothing to resume"
    return retry_in_n8n(client, s, lead_id)


def retry_in_n8n(client: httpx.Client, s: Settings, lead_id: str) -> str:
    if not s.n8n_webhook_url:
        raise HttpError("n8n is not configured here (LEADLOOM_N8N_WEBHOOK_URL is empty)", retryable=False)
    wait_for_n8n(client, s)
    send(
        client,
        "POST",
        f"{s.n8n_webhook_url}/leads/retry",
        what="n8n",
        attempts=3,
        json={"lead_id": lead_id},
        headers={"X-LeadLoom-Key": s.api_key},
    )
    return "retried"


def record(db: Pool, lead_id: str, actor: str, action: str, detail: dict) -> None:
    get(db, lead_id)
    with db.connection() as c:
        audit(c, lead_id, actor, action, detail)


def cost(db: Pool, lead_id: str) -> dict:
    with db.connection() as c:
        row = c.execute(
            """select coalesce(sum(cost_usd), 0)::float as usd, coalesce(sum(tokens_in), 0)::int as tokens_in,
                      coalesce(sum(tokens_out), 0)::int as tokens_out, coalesce(sum(ms), 0)::int as ms
               from steps where lead_id = %s""",
            (lead_id,),
        ).fetchone()
    return row or {}
