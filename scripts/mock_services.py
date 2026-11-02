"""Local stand-ins for the paid or signed-up services, so the app, n8n and the tests run
offline: a rule-based model speaking the chat-completions format with JSON-schema
output, the part of HubSpot's CRM API that LeadLoom uses, and a Slack incoming webhook.

    uv run python -m scripts.mock_services   # http://127.0.0.1:4041

A lead message containing "[flaky]" gets one 429 from the model first (to show
retries); "[broken]" gets an answer that fails the schema (to show the fallback)."""

import itertools
import json
import re
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

SPAM = ("seo", "page 1", "ranking", "outsourc", "per hour", "/hour", "followers", "lead list", "guarantee")
JOBS = ("hiring", "a job", "resume", "my cv", "internship", "looking for work")
PARTNER = ("partner", "reseller", "resell", "integration")
TROUBLE = ("can't", "cannot", "error", "not working", "broken", "bug", "help urgently")
RESEARCH = ("thesis", "student", "interview", "research")
BUYING = ("demo", "pricing", "price", "quote", "trial", "rfp", "cotiz", "demostra", "licenses")
SOON = ("next month", "this month", "january", "february", "q1", "before", "due", "enero", "next week")
TRADE = (
    "deliver",
    "logistic",
    "fleet",
    "dispatch",
    "distribu",
    "grocer",
    "hvac",
    "plumb",
    "waste",
    "moving",
    "3pl",
    "courier",
    "camiones",
    "technician",
    "garbage",
    "trucks",
    "vans",
)
FLEET_RE = re.compile(r"(\d[\d,]*)\s+(?:[a-z-]+\s+){0,2}(vans|vehicles|trucks|camiones|technicians|service vans)")


def assess(lead: dict) -> dict:
    text = f"{lead.get('message') or ''} {lead.get('job_title') or ''}".lower()
    site = " ".join(str(v) for v in (lead.get("company_website") or {}).values() if v).lower()
    m = FLEET_RE.search(text)
    fleet = int(m.group(1).replace(",", "")) if m else None
    crm = lead.get("crm") or {}
    has = lambda words, where=text: any(w in where for w in words)  # noqa: E731
    if has(SPAM):
        route = "spam"
    elif has(JOBS):
        route = "recruiting"
    elif has(PARTNER):
        route = "partnerships"
    elif crm.get("lifecycle_stage") == "customer" and has(TROUBLE):
        route = "support"
    elif has(RESEARCH):
        route = "nurture"
    elif (fleet or 0) >= 10 or has(BUYING):
        route = "sales"
    else:
        route = "nurture"
    fit = 30 + (20 if has(TRADE) or has(TRADE, site) else 0)
    if fleet is not None:
        fit = fit + 40 if fleet >= 25 else fit + 20 if fleet >= 10 else min(fit, 25)
    intent = 20 + (35 if has(BUYING) else 0) + (25 if has(SOON) else 0)
    if route == "spam":
        fit, intent = 5, 5
    reasons = [f"Mentions {fleet} vehicles." if fleet else "No fleet size given."]
    if has(BUYING):
        reasons.append("Asks for a demo, pricing or a trial.")
    return {
        "fit": min(fit, 100),
        "intent": min(intent, 100),
        "route": route,
        "confidence": 0.5 if len(lead.get("message") or "") < 40 else 0.85,
        "fleet_size": fleet,
        "reasons": reasons,
        "summary": f"{lead.get('company') or lead.get('name') or 'The lead'}: {route} lead (mock model).",
    }


def create_mock() -> FastAPI:
    app = FastAPI(title="LeadLoom mock services")
    state: dict = {}

    def reset() -> None:
        state.clear()
        state.update(
            flaky=set(),
            ids=itertools.count(1001),
            failures=[],
            slack=[],
            calls=[],
            assoc=[],
            objects={"contacts": {}, "companies": {}, "deals": {}, "notes": {}},
        )
        # An existing customer, to show dedupe and the existing-customer rule.
        state["objects"]["contacts"]["901"] = {
            "id": "901",
            "properties": {
                "email": "grace@greenline-grocers.example",
                "firstname": "Grace",
                "lastname": "Mendoza",
                "lifecyclestage": "customer",
                "hs_email_domain": "greenline-grocers.example",
            },
        }

    reset()

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/_reset")
    def reset_route() -> dict:
        reset()
        return {"ok": True}

    # ---- model -------------------------------------------------------------------

    @app.post("/llm/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        body = await request.json()
        prompt = body["messages"][-1]["content"]
        raw = prompt.split("\n<lead>\n", 1)[1].rsplit("\n</lead>", 1)[0].replace("<\\/", "</")
        lead = json.loads(raw)
        message = lead.get("message") or ""
        if "[flaky]" in message and prompt not in state["flaky"]:
            state["flaky"].add(prompt)
            return JSONResponse({"error": {"message": "Rate limit (mock)"}}, 429, headers={"retry-after": "1"})
        answer = assess(lead)
        if "[broken]" in message:
            answer.pop("route")
        usage = {"prompt_tokens": len(prompt) // 4, "completion_tokens": 120}
        return JSONResponse(
            {"choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}}], "usage": usage}
        )

    # ---- HubSpot -----------------------------------------------------------------

    @app.middleware("http")
    async def hubspot_guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/hubspot/crm/"):
            state["calls"].append(f"{request.method} {path}")
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return JSONResponse({"message": "Authentication credentials not found."}, 401)
            for f in state["failures"]:
                if f["times"] > 0 and f["match"] in path:
                    f["times"] -= 1
                    return JSONResponse({"status": "error", "message": "Injected failure"}, f["status"])
        return await call_next(request)

    @app.post("/hubspot/_fail")
    def fail(spec: dict) -> dict:
        state["failures"].append(
            {"match": spec["match"], "status": spec.get("status", 500), "times": spec.get("times", 1)}
        )
        return {"ok": True}

    @app.delete("/hubspot/_fail")
    def clear_failures() -> dict:
        state["failures"].clear()
        return {"ok": True}

    @app.get("/hubspot/_state")
    def hubspot_state() -> dict:
        return {"objects": state["objects"], "associations": state["assoc"], "calls": state["calls"]}

    def new(kind: str, properties: dict, associations: list | None = None) -> dict:
        rid = str(next(state["ids"]))
        if kind == "contacts" and properties.get("email"):
            properties["hs_email_domain"] = properties["email"].rpartition("@")[2].lower()
        obj = {"id": rid, "properties": properties, "associations": associations or []}
        state["objects"][kind][rid] = obj
        return obj

    @app.post("/hubspot/crm/v3/objects/{kind}/search")
    def search(kind: str, body: dict) -> dict:
        f = body["filterGroups"][0]["filters"][0]
        found = [
            {"id": o["id"], "properties": o["properties"]}
            for o in state["objects"][kind].values()
            if str(o["properties"].get(f["propertyName"], "")).lower() == str(f["value"]).lower()
        ][: body.get("limit", 10)]
        return {"total": len(found), "results": found}

    @app.post("/hubspot/crm/v3/objects/contacts/batch/upsert")
    def upsert(body: dict) -> dict:
        results = []
        for i in body["inputs"]:
            email = i["id"].lower()
            existing = next(
                (o for o in state["objects"]["contacts"].values() if o["properties"].get("email") == email), None
            )
            if existing:
                existing["properties"].update(i["properties"])
                results.append({"id": existing["id"], "properties": existing["properties"], "new": False})
            else:
                obj = new("contacts", dict(i["properties"]))
                results.append({"id": obj["id"], "properties": obj["properties"], "new": True})
        return {"status": "COMPLETE", "results": results}

    @app.post("/hubspot/crm/v3/objects/{kind}")
    def create(kind: str, body: dict) -> dict:
        obj = new(kind, dict(body.get("properties", {})), body.get("associations"))
        return {"id": obj["id"], "properties": obj["properties"]}

    @app.get("/hubspot/crm/v3/objects/{kind}/{rid}")
    def read(kind: str, rid: str) -> JSONResponse:
        obj = state["objects"][kind].get(rid)
        if not obj:
            return JSONResponse({"status": "error", "message": "Object not found"}, 404)
        return JSONResponse({"id": rid, "properties": obj["properties"]})

    @app.put("/hubspot/crm/v4/objects/{a}/{aid}/associations/default/{b}/{bid}")
    def associate(a: str, aid: str, b: str, bid: str) -> dict:
        state["assoc"].append([a, aid, b, bid])
        return {"status": "COMPLETE", "results": []}

    # ---- Slack -------------------------------------------------------------------

    @app.post("/slack/webhook", response_class=PlainTextResponse)
    async def slack(request: Request) -> str:
        state["slack"].append(await request.json())
        return "ok"

    @app.get("/slack/messages")
    def slack_messages() -> list:
        return state["slack"]

    return app


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4041
    uvicorn.run(create_mock(), host="127.0.0.1", port=port, log_level="warning")
