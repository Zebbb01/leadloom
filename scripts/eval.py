"""Scores every labeled lead in samples/leads.json with a real model, then grades the
final decision (after the rules) in code: no model judges another. Writes
evals/latest.json. With a Gemini API key from Google AI Studio (free tier):

    LLM_API_KEY=... uv run python -m scripts.eval

The company website comes from samples/sites (what the demo serves), and the CRM
context from each sample's "context", so only the model varies between runs."""

import json
import statistics
import sys
import time
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx

from app import llm, scoring
from app.config import Settings
from app.enrich import ROBOTS_AGENT, parse_html
from app.intake import Lead
from app.main import ROBOTS

ROOT = Path(__file__).resolve().parent.parent
DEMO_HOST = "https://leadloom.example"


def lead_of(x: dict) -> Lead:
    p = x["person"]
    return Lead(
        source=x["channel"],
        external_id=x["id"],
        email=p["email"],
        name=p["name"],
        company=p.get("company", ""),
        title=p.get("title", ""),
        phone=p.get("phone", ""),
        website=f"{DEMO_HOST}/sample-sites/{x['site']}" if x.get("site") else "",
        message=(f"{x['subject']}\n\n" if x.get("subject") and x["channel"] == "email" else "") + x["message"],
        honeypot=x.get("honeypot", False),
    )


def site_of(x: dict) -> dict | None:
    if not x.get("site"):
        return None
    url = f"{DEMO_HOST}/sample-sites/{x['site']}"
    robots = RobotFileParser()
    robots.parse(ROBOTS.splitlines())
    if not robots.can_fetch(ROBOTS_AGENT, url):
        return {"status": "robots_disallowed", "url": url}
    html = (ROOT / "samples" / "sites" / f"{x['site']}.html").read_text(encoding="utf-8")
    return {"status": "ok", "url": url, **parse_html(html)}


def grade(x: dict, d: dict) -> list[str]:
    e, failures = x["expect"], []
    if "route" in e and d["route"] not in e["route"]:
        failures.append(f"route {d['route']}, expected {'/'.join(e['route'])}")
    if "tier" in e and d["tier"] not in e["tier"]:
        failures.append(f"tier {d['tier']}, expected {'/'.join(e['tier'])}")
    if e.get("review") is not None and bool(d["review_reasons"]) != e["review"]:
        failures.append("sent to review" if d["review_reasons"] else "not sent to review")
    return failures


def run(samples: list[dict], client: httpx.Client, s: Settings, pause_s: float = 0.0) -> dict:
    results = []
    for x in samples:
        lead = lead_of(x)
        dedupe = {"existing": None, "possible_duplicates": [], "open_deal_id": None, **x.get("context", {})}
        site = site_of(x)
        started = time.perf_counter()
        usage = {"tokens_in": 0, "tokens_out": 0}
        assessment, error = None, None
        if not scoring.junk_rule(lead):
            try:
                assessment, usage = llm.complete(
                    client, s, scoring.SYSTEM_PROMPT, scoring.user_prompt(lead, dedupe, site), scoring.Assessment
                )
            except llm.LlmError as e:
                error, usage = str(e), e.usage
        ms = round((time.perf_counter() - started) * 1000)
        if pause_s and not scoring.junk_rule(lead):
            time.sleep(pause_s)
        d = scoring.decide(lead, assessment, dedupe, error)
        failures = grade(x, d)
        results.append(
            {
                "id": x["id"],
                "pass": not failures,
                "failures": failures,
                "route": d["route"],
                "tier": d["tier"],
                "score": d["score"],
                "review": d["review_reasons"],
                "rules": [o["rule"] for o in d["overrides"]],
                "model": d["model"],
                "model_error": error,
                "tokens_in": usage["tokens_in"],
                "tokens_out": usage["tokens_out"],
                "cost_usd": llm.cost_usd(s.llm_model, usage["tokens_in"], usage["tokens_out"]),
                "ms": ms,
            }
        )
        r = results[-1]
        print(
            f"{'PASS' if r['pass'] else 'FAIL'}  {x['id']:<22} {r['route']:<12} {r['tier']:<5} {r['score']:>3}"
            f"  {'; '.join(failures)}{'  model error: ' + error if error else ''}"
        )
    called = [r for r in results if r["model"] or r["model_error"]]
    return {
        "model": s.llm_model,
        "endpoint": s.llm_endpoint,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "passed": sum(r["pass"] for r in results),
        "total": len(results),
        "model_errors": sum(bool(r["model_error"]) for r in results),
        "rules_changed_route": sum(bool(r["model"]) and r["model"]["route"] != r["route"] for r in results),
        "avg_tokens": round(statistics.mean(r["tokens_in"] + r["tokens_out"] for r in called)) if called else 0,
        "avg_cost_usd": statistics.mean(r["cost_usd"] for r in called) if called else 0,
        "median_ms": statistics.median(r["ms"] for r in called) if called else 0,
        "results": results,
    }


def main() -> None:
    s = Settings.from_env()
    if not s.llm_api_key:
        sys.exit("Set LLM_API_KEY (a Gemini API key from https://aistudio.google.com/apikey).")
    samples = json.loads((ROOT / "samples" / "leads.json").read_text(encoding="utf-8"))
    # The free tier allows a handful of requests a minute, so the run is paced.
    with httpx.Client() as client:
        summary = run(samples, client, s, pause_s=float(sys.argv[1]) if len(sys.argv) > 1 else 6.0)
    print(
        f"\n{summary['passed']}/{summary['total']} passed on {s.llm_model}. Per lead: {summary['avg_tokens']} tokens,"
        f" ${summary['avg_cost_usd']:.6f} at list price, median {summary['median_ms'] / 1000:.1f} s."
        f" Rules changed the model's route on {summary['rules_changed_route']} leads."
    )
    out = ROOT / "evals" / "latest.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
