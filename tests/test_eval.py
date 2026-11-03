import json
from pathlib import Path

import httpx

from scripts import eval as ev
from tests.conftest import make_settings

SAMPLES = json.loads((Path(__file__).resolve().parent.parent / "samples" / "leads.json").read_text(encoding="utf-8"))


def test_grade_checks_route_tier_and_review():
    x = {"expect": {"route": ["sales"], "tier": ["hot", "warm"], "review": False}}
    assert ev.grade(x, {"route": "sales", "tier": "warm", "review_reasons": []}) == []
    assert ev.grade(x, {"route": "nurture", "tier": "cold", "review_reasons": ["why"]}) == [
        "route nurture, expected sales",
        "tier cold, expected hot/warm",
        "sent to review",
    ]


def test_samples_are_well_formed():
    ids = [x["id"] for x in SAMPLES]
    assert len(ids) == len(set(ids)) >= 20
    for x in SAMPLES:
        assert x["channel"] in ("form", "email", "booking") and x["expect"]
        ev.lead_of(x)
        if x.get("site"):
            assert ev.site_of(x)["status"] in ("ok", "robots_disallowed")
    assert ev.site_of(next(x for x in SAMPLES if x["id"] == "quiet-harbor-booking"))["status"] == "robots_disallowed"


def test_run_scores_every_sample(router):
    s = make_settings("", llm_endpoint="http://mock/llm")
    summary = ev.run(SAMPLES, httpx.Client(transport=router), s)
    assert summary["total"] == len(SAMPLES) and summary["model_errors"] == 0
    junk = [r for r in summary["results"] if r["id"] in ("honeypot", "disposable")]
    assert all(r["route"] == "spam" and r["tokens_in"] == 0 for r in junk)
    assert len(router.to("mock", "/llm")) == len(SAMPLES) - 2
