import psycopg
import pytest

from app import pipeline
from app.http import HttpError
from app.intake import Lead

HOT = "We run 140 delivery vans and want a demo and pricing before February."


def lead(**kw) -> Lead:
    return Lead(
        **{
            "source": "form",
            "external_id": "f-1",
            "email": "maria@brightpath.example.com",
            "name": "Maria Santos",
            "company": "BrightPath Logistics",
            "website": "http://sites.test/brightpath-logistics",
            "message": HOT,
            **kw,
        }
    )


def qualify(db, settings, clients, lead_id):
    return pipeline.qualify(db, settings, lead_id, web=clients["web"], model=clients["model"], crm=clients["crm"])


def actions(db, lead_id) -> list[str]:
    with db.connection() as c:
        return [r["action"] for r in c.execute("select action from audit where lead_id = %s order by id", (lead_id,))]


def test_store_is_idempotent_per_delivery(db):
    first, created = pipeline.store(db, lead())
    again, created_again = pipeline.store(db, lead())
    other, _ = pipeline.store(db, lead(external_id="f-2"))
    assert created and not created_again and first == again != other
    assert actions(db, first) == ["received", "redelivered"]


def test_qualify_runs_each_step_once(db, settings, clients, router):
    lead_id, _ = pipeline.store(db, lead())
    result = qualify(db, settings, clients, lead_id)
    d = result["decision"]
    assert (result["next"], d["route"], d["tier"]) == ("sync", "sales", "hot")
    assert d["site"]["status"] == "ok" and d["site"]["site_name"] == "BrightPath Logistics"
    assert len(router.to("mock", "/llm")) == 1
    again = qualify(db, settings, clients, lead_id)
    assert again["next"] == "sync" and len(router.to("mock", "/llm")) == 1
    assert actions(db, lead_id) == ["received", "qualified"]
    assert pipeline.cost(db, lead_id)["usd"] > 0


def test_flaky_model_is_retried_and_a_broken_answer_falls_back(db, settings, clients, router):
    flaky, _ = pipeline.store(db, lead(message=f"{HOT} [flaky]"))
    assert qualify(db, settings, clients, flaky)["decision"]["model"] is not None
    assert len(router.to("mock", "/llm")) == 2
    broken, _ = pipeline.store(db, lead(external_id="f-2", message=f"{HOT} [broken]"))
    result = qualify(db, settings, clients, broken)
    assert result["next"] == "review"
    assert result["decision"]["overrides"][0]["rule"] == "model_unavailable"


def test_junk_never_reaches_the_model(db, settings, clients, router):
    lead_id, _ = pipeline.store(db, lead(honeypot=True))
    result = qualify(db, settings, clients, lead_id)
    assert result["next"] == "discard" and router.to("mock", "/llm") == []
    assert pipeline.get(db, lead_id)["status"] == "discarded"


def test_daily_model_cap_falls_back_to_rules(db, settings, clients, router):
    capped = settings.__class__(**{**settings.__dict__, "daily_llm_cap": 0})
    lead_id, _ = pipeline.store(db, lead())
    result = qualify(db, capped, clients, lead_id)
    assert result["next"] == "review" and router.to("mock", "/llm") == []


def test_dedupe_finds_the_same_person_under_another_address(db, settings, clients):
    first, _ = pipeline.store(db, lead())
    qualify(db, settings, clients, first)
    second, _ = pipeline.store(
        db, lead(external_id="f-2", email="m.santos@brightpath.example.com", name="Maria D. Santos")
    )
    result = qualify(db, settings, clients, second)
    assert result["next"] == "review"
    assert result["decision"]["dedupe"]["possible_duplicates"][0]["email"] == "maria@brightpath.example.com"
    third, _ = pipeline.store(db, lead(external_id="f-3"))
    assert qualify(db, settings, clients, third)["decision"]["dedupe"]["existing"]["earlier_leads"] == 1


def test_sync_writes_hubspot_once_and_claims_the_alert_once(db, settings, clients, router):
    lead_id, _ = pipeline.store(db, lead())
    qualify(db, settings, clients, lead_id)
    result = pipeline.sync(db, settings, lead_id, clients["crm"])
    h = result["hubspot"]
    assert result["alert"] and "Hot lead" in result["alert"]["text"] and "Maria Santos" in result["alert"]["text"]
    assert h["urls"]["deal"] == f"https://app.hubspot.com/contacts/4242/record/0-3/{h['deal_id']}"
    state = router.hubspot()
    contact = state["objects"]["contacts"][h["contact_id"]]["properties"]
    assert (contact["lifecyclestage"], contact["hs_lead_status"], contact["firstname"]) == (
        "salesqualifiedlead",
        "NEW",
        "Maria",
    )
    deal = state["objects"]["deals"][h["deal_id"]]
    assert {a["types"][0]["associationTypeId"] for a in deal["associations"]} == {3, 5}
    note = state["objects"]["notes"][h["note_id"]]
    assert "score" in note["properties"]["hs_note_body"] and len(note["associations"]) == 3
    assert ["contacts", h["contact_id"], "companies", h["company_id"]] in state["associations"]
    again = pipeline.sync(db, settings, lead_id, clients["crm"])
    assert again["alert"] is None and again["hubspot"] == h


def test_a_failed_sync_resumes_without_duplicates(db, settings, clients, router):
    lead_id, _ = pipeline.store(db, lead())
    qualify(db, settings, clients, lead_id)
    router.mock.post("/hubspot/_fail", json={"match": "/objects/notes", "status": 503, "times": 4})
    with pytest.raises(HttpError):
        pipeline.sync(db, settings, lead_id, clients["crm"])
    row = pipeline.get(db, lead_id)
    assert row["status"] == "failed" and "503" in row["error"]
    result = pipeline.sync(db, settings, lead_id, clients["crm"])
    assert result["status"] == "synced" and result["alert"]
    objects = router.hubspot()["objects"]
    assert (len(objects["companies"]), len(objects["deals"]), len(objects["notes"])) == (1, 1, 1)
    assert actions(db, lead_id)[-2:] == ["sync_failed", "synced"]


def test_existing_customer_keeps_lifecycle_and_open_deal_is_reused(db, settings, clients, router):
    grace = lead(
        email="grace@greenline-grocers.example.com",
        name="Grace Mendoza",
        company="Greenline Grocers",
        website="",
        message="We're adding 50 vehicles in January, please quote the licenses.",
    )
    first, _ = pipeline.store(db, grace)
    assert qualify(db, settings, clients, first)["decision"]["dedupe"]["existing"]["lifecyclestage"] == "customer"
    h1 = pipeline.sync(db, settings, first, clients["crm"])["hubspot"]
    assert h1["contact_id"] == "901"
    assert router.hubspot()["objects"]["contacts"]["901"]["properties"]["lifecyclestage"] == "customer"
    second, _ = pipeline.store(db, grace.model_copy(update={"external_id": "f-2"}))
    qualify(db, settings, clients, second)
    h2 = pipeline.sync(db, settings, second, clients["crm"])["hubspot"]
    assert h2["deal_id"] == h1["deal_id"] and h2["deal_reused"]


def test_review_hold_approve_and_reject(db, settings, clients):
    lead_id, _ = pipeline.store(db, lead(message="Interested. Call me."))
    assert qualify(db, settings, clients, lead_id)["next"] == "review"
    with pytest.raises(pipeline.Conflict):
        pipeline.hold(db, settings, lead_id, "http://evil.test/webhook-waiting/1")
    pipeline.hold(db, settings, lead_id, "http://n8n.test/webhook-waiting/7?signature=abc")
    result = pipeline.review(db, lead_id, "reviewer", approve=True, route="sales", score=82, note="Called them.")
    assert result == {
        "lead_id": lead_id,
        "status": "approved",
        "resume_url": "http://n8n.test/webhook-waiting/7?signature=abc",
    }
    d = pipeline.get(db, lead_id)["decision"]
    assert (d["score"], d["tier"], d["next"], d["alert"]) == (82, "hot", "sync", True)
    assert d["reviewed"]["changes"]["score"][1] == 82
    with pytest.raises(pipeline.Conflict):
        pipeline.review(db, lead_id, "reviewer", approve=False)
    other, _ = pipeline.store(db, lead(external_id="f-2", message="Interested. Call me."))
    qualify(db, settings, clients, other)
    pipeline.hold(db, settings, other, "http://n8n.test/webhook-waiting/8")
    assert pipeline.review(db, other, "reviewer", approve=False)["status"] == "rejected"
    assert actions(db, lead_id) == ["received", "qualified", "awaiting_review", "approved"]


def test_resume_falls_back_to_the_retry_webhook(settings, clients, router):
    n8n = clients["n8n"]
    assert pipeline.resume_n8n(n8n, settings, "id-1", "http://n8n.test/webhook-waiting/7", True) == "resumed"
    # A run that is no longer waiting answers 404; an approved lead then goes through the retry webhook.
    router.replies["gone.test"] = (404, {"message": "not waiting"})
    assert pipeline.resume_n8n(n8n, settings, "id-1", "http://gone.test/webhook-waiting/7", True) == "retried"
    sent = router.to("n8n.test", "/webhook/leads/retry")
    assert len(sent) == 1 and sent[0].headers["x-leadloom-key"] == "test-key"
    assert (
        pipeline.resume_n8n(n8n, settings, "id-1", "http://gone.test/webhook-waiting/7", False) == "nothing to resume"
    )
    router.replies["n8n.test"] = (502, {})
    with pytest.raises(HttpError):
        pipeline.resume_n8n(n8n, settings, "id-1", "http://n8n.test/webhook-waiting/7", True)


def test_audit_log_is_append_only(db):
    lead_id, _ = pipeline.store(db, lead())
    with db.connection() as c, pytest.raises(psycopg.errors.RaiseException):
        c.execute("update audit set action = 'edited' where lead_id = %s", (lead_id,))


def test_record_links_open_in_the_account_data_centre():
    from app.hubspot import app_host, record_url

    assert [app_host(t) for t in ("pat-na1-x", "pat-na2-x", "pat-eu1-x", "test")] == [
        "app.hubspot.com",
        "app-na2.hubspot.com",
        "app-eu1.hubspot.com",
        "app.hubspot.com",
    ]
    assert (
        record_url("42", "deal", "7", "app-na2.hubspot.com") == "https://app-na2.hubspot.com/contacts/42/record/0-3/7"
    )
