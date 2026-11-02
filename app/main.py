"""LeadLoom's HTTP service: the /v1 API that n8n calls, and the pages where a person
reviews leads (and visitors try the demo)."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import db as database
from . import hubspot, intake, pipeline
from .config import Settings
from .http import HttpError, send
from .intake import booking_signature
from .scoring import ROUTES

HERE = Path(__file__).parent
SAMPLES_DIR = HERE.parent / "samples"
SAMPLES = {s["id"]: s for s in json.loads((SAMPLES_DIR / "leads.json").read_text(encoding="utf-8"))}
SLUG_RE = re.compile(r"^[a-z0-9-]{1,60}$")
COOKIE_WS, COOKIE_REVIEWER = "ll_ws", "ll_reviewer"
CHANNEL_NAMES = {"form": "Website form", "email": "Inbound email", "booking": "Booking"}
STATUS_NAMES = {
    "received": "Received",
    "qualified": "Qualified",
    "awaiting_review": "Needs review",
    "approved": "Approved",
    "rejected": "Rejected",
    "synced": "Synced",
    "discarded": "Discarded",
    "failed": "Sync failed",
}
SITE_NAMES = {
    "robots_disallowed": "Not read: the site's robots.txt asks LeadLoomBot to stay out.",
    "robots_unreachable": "Not read: robots.txt could not be fetched, which RFC 9309 treats as a refusal.",
    "blocked": "Not fetched: the address points to a private or reserved network.",
    "unreachable": "The site could not be reached.",
    "not_html": "The site did not return a web page.",
}
ROBOTS = """User-agent: LeadLoomBot
Disallow: /sample-sites/quiet-harbor-movers
Allow: /sample-sites/

User-agent: *
Disallow: /
"""


def reviewer_token(key: str) -> str:
    return hmac.new(key.encode(), b"leadloom-reviewer", hashlib.sha256).hexdigest()


def create_app(s: Settings | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    """transport replaces the network for every outbound client (used by the tests)."""
    s = s or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = database.connect(s.database_url)
        with app.state.db.connection() as c:
            c.execute("delete from leads where workspace is not null and created_at < now() - interval '7 days'")
        app.state.web = httpx.Client(transport=transport)
        app.state.model = httpx.Client(transport=transport)
        app.state.n8n = httpx.Client(transport=transport, timeout=httpx.Timeout(60.0, connect=10.0))
        app.state.crm = hubspot.client(s.hubspot_token, s.hubspot_base_url, transport) if s.hubspot_token else None
        yield
        for client in (app.state.web, app.state.model, app.state.n8n, app.state.crm):
            if client:
                client.close()
        app.state.db.close()

    app = FastAPI(title="LeadLoom", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.s = s
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        ROUTES=ROUTES, channel_names=CHANNEL_NAMES, status_names=STATUS_NAMES, site_names=SITE_NAMES
    )
    templates.env.filters["usd"] = lambda x: f"${x:.5f}" if x else "$0"
    templates.env.filters["when"] = lambda dt, time_only=False: dt.astimezone(UTC).strftime(
        "%H:%M:%S" if time_only else "%b %d, %H:%M UTC"
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def workspace(request: Request, call_next):
        ws = request.cookies.get(COOKIE_WS, "")
        fresh = not intake.WORKSPACE_RE.match(ws)
        request.state.workspace = secrets.token_hex(16) if fresh else ws
        response = await call_next(request)
        if fresh and not request.url.path.startswith(("/v1/", "/static/", "/health", "/sample-sites/", "/robots")):
            response.set_cookie(
                COOKIE_WS,
                request.state.workspace,
                max_age=30 * 86400,
                httponly=True,
                samesite="lax",
                secure=s.public_url.startswith("https"),
            )
        return response

    @app.exception_handler(pipeline.NotFound)
    async def not_found(request: Request, e: Exception) -> JSONResponse:
        return JSONResponse({"error": "No such lead."}, 404)

    @app.exception_handler(pipeline.Conflict)
    async def conflict(request: Request, e: Exception) -> JSONResponse:
        return JSONResponse({"error": str(e)}, 409)

    @app.exception_handler(HttpError)
    async def upstream(request: Request, e: Exception) -> JSONResponse:
        return JSONResponse({"error": str(e)}, 502)

    # ---- API for n8n -------------------------------------------------------------

    def require_key(request: Request) -> None:
        given = request.headers.get("authorization", "").encode()
        if not s.api_key or not hmac.compare_digest(given, f"Bearer {s.api_key}".encode()):
            raise HTTPException(401, "Missing or wrong API key.")

    api = [Depends(require_key)]

    @app.post("/v1/intake/{source}", dependencies=api)
    async def intake_lead(source: str, request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > 256_000:
            return JSONResponse({"error": "The body is too large."}, 413)
        try:
            lead = intake.parse(source, body, request.headers, s)
        except intake.Ignored as e:
            return JSONResponse({"ignored": str(e)}, 202)
        except intake.IntakeError as e:
            return JSONResponse({"error": str(e)}, e.status)
        lead_id, created = await run_in_threadpool(pipeline.store, request.app.state.db, lead)
        return JSONResponse({"lead_id": lead_id, "duplicate": not created}, 201 if created else 200)

    @app.post("/v1/leads/{lead_id}/qualify", dependencies=api)
    def qualify(lead_id: uuid.UUID, request: Request) -> dict:
        st = request.app.state
        return pipeline.qualify(st.db, s, str(lead_id), web=st.web, model=st.model, crm=st.crm)

    class Hold(BaseModel):
        resume_url: str = Field(max_length=2000)

    @app.post("/v1/leads/{lead_id}/hold", dependencies=api)
    def hold(lead_id: uuid.UUID, body: Hold, request: Request) -> dict:
        return pipeline.hold(request.app.state.db, s, str(lead_id), body.resume_url)

    @app.post("/v1/leads/{lead_id}/sync", dependencies=api)
    def sync(lead_id: uuid.UUID, request: Request) -> dict:
        return pipeline.sync(request.app.state.db, s, str(lead_id), request.app.state.crm)

    class Event(BaseModel):
        action: str = Field(pattern=r"^[a-z_]{1,40}$")
        detail: dict = {}

    @app.post("/v1/leads/{lead_id}/events", dependencies=api)
    def event(lead_id: uuid.UUID, body: Event, request: Request) -> dict:
        pipeline.record(request.app.state.db, str(lead_id), "n8n", body.action, body.detail)
        return {"ok": True}

    # ---- pages -------------------------------------------------------------------

    def is_reviewer(request: Request) -> bool:
        token = request.cookies.get(COOKIE_REVIEWER, "")
        return bool(s.reviewer_key) and hmac.compare_digest(token, reviewer_token(s.reviewer_key))

    def visible(request: Request, lead_id: uuid.UUID) -> dict:
        row = pipeline.get(request.app.state.db, str(lead_id))
        if not is_reviewer(request) and row["workspace"] != request.state.workspace:
            raise HTTPException(404, "No such lead.")
        return row

    def page(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"reviewer": is_reviewer(request), "s": s, **context})

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        samples = [x for x in SAMPLES.values() if x.get("demo")]
        return page(request, "home.html", samples=samples, error=request.query_params.get("error"))

    @app.get("/leads", response_class=HTMLResponse)
    def leads(request: Request) -> HTMLResponse:
        with request.app.state.db.connection() as c:
            rows = c.execute(
                """select l.id, l.source, l.status, l.email, l.lead, l.decision, l.created_at, l.workspace,
                          coalesce((select sum(cost_usd) from steps where lead_id = l.id), 0)::float as cost
                   from leads l where %s or l.workspace = %s
                   order by (l.status = 'awaiting_review') desc, l.created_at desc limit 100""",
                (is_reviewer(request), request.state.workspace),
            ).fetchall()
        return page(request, "leads.html", rows=rows)

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_page(lead_id: uuid.UUID, request: Request) -> HTMLResponse:
        row = visible(request, lead_id)
        db = request.app.state.db
        with db.connection() as c:
            events = c.execute("select * from audit where lead_id = %s order by id", (str(lead_id),)).fetchall()
            steps = c.execute("select * from steps where lead_id = %s order by created_at", (str(lead_id),)).fetchall()
        busy = row["status"] in ("received", "approved") or (
            row["status"] == "qualified" and row["decision"]["next"] in ("sync", "review")
        )
        stuck = busy and row["updated_at"] < datetime.now(UTC) - timedelta(minutes=2)
        return page(
            request,
            "lead.html",
            row=row,
            lead=row["lead"],
            d=row["decision"],
            events=events,
            steps=steps,
            cost=pipeline.cost(db, str(lead_id)),
            busy=busy and not stuck,
            stuck=stuck,
            message=request.query_params.get("message"),
        )

    @app.post("/leads/{lead_id}/review")
    def review(
        lead_id: uuid.UUID,
        request: Request,
        action: Annotated[str, Form()],
        route: Annotated[str, Form()] = "",
        score: Annotated[str, Form()] = "",
        note: Annotated[str, Form(max_length=1000)] = "",
    ) -> RedirectResponse:
        visible(request, lead_id)
        st = request.app.state
        actor = "reviewer" if is_reviewer(request) else "demo visitor"
        try:
            result = pipeline.review(
                st.db,
                str(lead_id),
                actor,
                approve=action == "approve",
                route=route if route in ROUTES else None,
                score=max(0, min(100, int(score))) if score.strip().isdigit() else None,
                note=note.strip(),
            )
        except pipeline.Conflict as e:
            return RedirectResponse(f"/leads/{lead_id}?message={e}", 303)
        try:
            how = pipeline.resume_n8n(st.n8n, s, str(lead_id), result["resume_url"], result["status"] == "approved")
            message = {
                "resumed": "Saved. n8n picked the lead up again.",
                "retried": "Saved. n8n is syncing it.",
                "nothing to resume": "Saved.",
            }[how]
        except HttpError as e:
            message = f"Saved, but n8n could not be reached ({e}). Use Retry."
        return RedirectResponse(f"/leads/{lead_id}?message={message}", 303)

    @app.post("/leads/{lead_id}/retry")
    def retry(lead_id: uuid.UUID, request: Request) -> RedirectResponse:
        visible(request, lead_id)
        try:
            pipeline.retry_in_n8n(request.app.state.n8n, s, str(lead_id))
            message = "Sent back through n8n."
        except HttpError as e:
            message = f"n8n could not be reached ({e})."
        return RedirectResponse(f"/leads/{lead_id}?message={message}", 303)

    @app.get("/signin", response_class=HTMLResponse)
    def signin_page(request: Request) -> HTMLResponse:
        return page(request, "signin.html", error=None)

    @app.post("/signin", response_model=None)
    def signin(request: Request, key: Annotated[str, Form(max_length=200)]) -> HTMLResponse | RedirectResponse:
        if not s.reviewer_key or not hmac.compare_digest(key.encode(), s.reviewer_key.encode()):
            return page(request, "signin.html", error="That key is not right.")
        response = RedirectResponse("/leads", 303)
        response.set_cookie(
            COOKIE_REVIEWER,
            reviewer_token(s.reviewer_key),
            max_age=7 * 86400,
            httponly=True,
            samesite="lax",
            secure=s.public_url.startswith("https"),
        )
        return response

    @app.post("/signout")
    def signout() -> RedirectResponse:
        response = RedirectResponse("/", 303)
        response.delete_cookie(COOKIE_REVIEWER)
        return response

    # ---- demo --------------------------------------------------------------------

    @app.post("/demo/send")
    def demo_send(
        request: Request,
        sample: Annotated[str, Form()],
        channel: Annotated[str, Form()] = "",
        message: Annotated[str, Form(max_length=2000)] = "",
    ) -> RedirectResponse:
        x = SAMPLES.get(sample)
        if not x or not x.get("demo"):
            raise HTTPException(404, "No such sample.")
        ws = request.state.workspace
        with request.app.state.db.connection() as c:
            sent = c.execute(
                "select count(*) as n from leads where workspace = %s and created_at > now() - interval '1 day'", (ws,)
            ).fetchone()["n"]
        if sent >= s.demo_daily_per_workspace:
            return RedirectResponse("/?error=This browser has sent today's demo leads. Try again tomorrow.", 303)
        if not s.n8n_webhook_url:
            return RedirectResponse("/?error=n8n is not configured here (N8N_WEBHOOK_URL).", 303)
        channel = channel if channel in intake.SOURCES else x["channel"]
        body, headers = demo_payload(s, x, channel, message.strip() or x["message"], ws)
        try:
            r = send(
                request.app.state.n8n,
                "POST",
                f"{s.n8n_webhook_url}/leads/{channel}",
                what="n8n",
                content=body,
                headers=headers,
                attempts=2,
            )
            lead_id = r.json()["lead_id"]
        except (HttpError, ValueError, KeyError) as e:
            return RedirectResponse(f"/?error=The lead did not get through n8n ({str(e)[:200]}).", 303)
        return RedirectResponse(f"/leads/{lead_id}", 303)

    @app.get("/sample-sites/{slug}", response_class=HTMLResponse)
    def sample_site(slug: str) -> FileResponse:
        path = SAMPLES_DIR / "sites" / f"{slug}.html"
        if not SLUG_RE.match(slug) or not path.is_file():
            raise HTTPException(404, "No such site.")
        return FileResponse(path, media_type="text/html; charset=utf-8")

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        return ROBOTS

    @app.get("/health")
    def health(request: Request) -> dict:
        with request.app.state.db.connection() as c:
            c.execute("select 1")
        return {"ok": True}

    return app


def demo_payload(s: Settings, x: dict, channel: str, message: str, ws: str) -> tuple[bytes, dict]:
    """A sample lead in the format its channel really sends, signed like the real
    sender, so the demo takes the same path through n8n as a live lead."""
    p = x["person"]
    website = f"{s.public_url}/sample-sites/{x['site']}" if x.get("site") else p.get("website", "")
    now = datetime.now(UTC)
    if channel == "form":
        data: dict = {
            "submission_id": uuid.uuid4().hex,
            "name": p["name"],
            "email": p["email"],
            "company": p.get("company", ""),
            "website": website,
            "phone": p.get("phone", ""),
            "title": p.get("title", ""),
            "message": message,
            "workspace": ws,
        }
        return json.dumps(data).encode(), {"Content-Type": "application/json"}
    if channel == "email":
        data = {
            "FromFull": {"Email": p["email"], "Name": p["name"]},
            "From": p["email"],
            "FromName": p["name"],
            "Subject": x.get("subject") or "Question about Kestrel Fleet",
            "TextBody": message,
            "MessageID": str(uuid.uuid4()),
            "MailboxHash": f"demo-{ws}",
            "Date": now.isoformat(),
        }
        auth = "Basic " + base64.b64encode(f"postmark:{s.inbound_email_secret}".encode()).decode()
        return json.dumps(data).encode(), {"Content-Type": "application/json", "Authorization": auth}
    start = (now + timedelta(days=2)).replace(hour=9, minute=30, second=0, microsecond=0)
    data = {
        "triggerEvent": "BOOKING_CREATED",
        "createdAt": now.isoformat(),
        "payload": {
            "uid": uuid.uuid4().hex[:22],
            "title": f"Kestrel Fleet demo with {p['name']}",
            "type": "demo",
            "startTime": start.isoformat().replace("+00:00", "Z"),
            "endTime": (start + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "attendees": [{"email": p["email"], "name": p["name"], "timeZone": p.get("tz", "UTC")}],
            "responses": {
                "name": {"value": p["name"]},
                "email": {"value": p["email"]},
                "company": {"value": p.get("company", "")},
                "website": {"value": website},
                "notes": {"value": message},
            },
            "additionalNotes": message,
            "metadata": {"workspace": ws},
            "status": "ACCEPTED",
        },
    }
    body = json.dumps(data).encode()
    return body, {
        "Content-Type": "application/json",
        "X-Cal-Signature-256": booking_signature(body, s.cal_webhook_secret),
    }
