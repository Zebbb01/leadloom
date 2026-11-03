# LeadLoom

[![CI](https://github.com/Zebbb01/leadloom/actions/workflows/ci.yml/badge.svg)](https://github.com/Zebbb01/leadloom/actions/workflows/ci.yml)

Leads in; qualified, deduplicated and in the CRM out. LeadLoom is a lead-intake and CRM automation system
built from a self-hosted **n8n** workflow and a **Python (FastAPI)** service. n8n receives every lead (a
website form, inbound email, a booked meeting) and orchestrates; the Python service does what n8n does
badly: it checks who the lead already is, reads their company's website, and scores and routes them with an
LLM whose answer is validated against a schema and can be overruled by rules in code. Results go to
**HubSpot's free CRM** as a contact, company, deal and a note that explains the score. Hot leads post to
Slack; unsure ones wait on a review page for a person.

Every webhook is idempotent, every outside call is retried with backoff, and each lead has an append-only
audit log and a cost.

The seller is **Kestrel Fleet**, a fictional route-planning SaaS for delivery fleets; its leads are made up.

![A booked demo: score 86, hot, the booked-meeting rule raised intent, synced to HubSpot, alert sent](docs/screenshots/lead-synced.png)

## What it does

- **Three ways in, one shape out.** A website form (JSON or a plain form post), inbound email in Postmark's
  JSON format, and Cal.com `BOOKING_CREATED` webhooks. n8n forwards the raw body, so the service checks
  Cal.com's HMAC signature over the exact bytes and Postmark's basic auth before anything is stored.
- **Idempotent webhooks.** A lead is stored once per delivery (`source` + Cal.com `uid`, Postmark
  `MessageID`, or the form's `submission_id`). A redelivered webhook gets the same lead back and stops there,
  so it never scores, syncs or alerts twice.
- **Dedupe against existing contacts**: the same email in earlier leads or in HubSpot; the same person under
  another address (same company domain and a matching name, or the same phone number), which goes to review.
  An open deal from an earlier lead is reused instead of opening a second one.
- **Company enrichment** from the lead's website or work-email domain, with `httpx`: robots.txt first (an
  unreachable robots.txt means no fetch, as RFC 9309 says), 3-second connect and 6-second read timeouts, a
  512 KB cap, and no requests to private or reserved addresses, checked again on every redirect.
- **Scoring and routing with an LLM** (Gemini API free tier by default, any OpenAI-compatible endpoint):
  fit and intent from 0 to 100, a route (sales, partnerships, support, recruiting, nurture, spam), a
  confidence, reasons and a summary, requested as a strict JSON schema and validated with Pydantic. An answer
  that fails validation falls back to the rules and goes to a person.
- **Rules in code have the last word** (below). Each rule that changes the outcome is recorded with its
  reason, shown on the review page and written into the HubSpot note.
- **A person decides when it is unsure.** n8n parks the run in a Wait node; the review page shows why and lets
  the reviewer change the route or score, approve or reject. Approval resumes the same n8n run.
- **HubSpot sync**: company (by domain), contact (upsert by email; the lifecycle stage is set only for new
  contacts), a deal for sales leads, and a note with the score, the reasons and the rules applied. Each call
  is a durable step: a retry after a failure resumes at the step that failed, so nothing is created twice.
- **Retries with backoff** on every outside call (0.5, 1, 2 s, or the service's `Retry-After`), n8n's own
  retries on each HTTP node, and a Retry button that sends a failed lead back through n8n.
- **Cost per lead**: tokens for each model call at the model's list price. On the free tier the real cost is
  zero; the figure shows what a paid plan would charge.

![An unsure lead waiting for a person: why it needs one, and the route and score to edit before approving](docs/screenshots/lead-review.png)

## How it works

```mermaid
flowchart LR
  F[Website form] --> N
  E[Postmark inbound email] --> N
  C[Cal.com booking] --> N
  N[n8n workflow] -->|raw body| A[LeadLoom API<br/>FastAPI]
  A --> P[(PostgreSQL<br/>leads, steps, audit log)]
  A -->|score| M[Gemini API<br/>free tier]
  A -->|robots.txt, home page| W[Company website]
  A -->|contact, company, deal, note| H[HubSpot free CRM]
  N -->|hot lead| S[Slack]
  R[Reviewer] <-->|review page| A
  A -->|approve: resume the waiting run| N
```

![The workflow in n8n](docs/screenshots/n8n-workflow.png)

| n8n does | The Python service does |
| --- | --- |
| Receives the three webhooks and answers each sender with the API's status | Checks signatures and credentials, parses each format, stores each delivery once |
| Calls `qualify`, then branches on the next step: review, sync or stop | Dedupes, enriches, scores with the LLM, applies the rules, decides the next step |
| Waits for a person (Wait node, resumed by the review page) | Shows the review page and records the decision |
| Calls `sync`, then sends the Slack alert when the API says so | Writes HubSpot as durable steps and claims the alert once |
| Retries each HTTP call; re-enters the flow from the Retry webhook | Locks each lead while it works, so overlapping retries wait their turn |

The workflow is committed as JSON (`n8n/workflows/lead-intake.json`). Locally it is imported and published
before n8n starts; in Azure a Container Apps job does the same on each deploy.

## The rules

| Rule | When | Effect |
| --- | --- | --- |
| `honeypot` | The form's hidden `nickname` field is filled in | Spam; the model is not called |
| `disposable_email` | A throwaway address (mailinator.com and others) | Spam; the model is not called |
| `existing_customer` | HubSpot says the contact is a customer and the model said spam or nurture | Route becomes support |
| `booked_meeting` | The lead booked a meeting | Intent at least 80; nurture becomes sales |
| `personal_email` | A personal address (gmail.com and others) and no company website | Fit capped at 50 |
| `model_unavailable` | No model, the daily cap reached, or an answer that fails the schema | Scored by rules; goes to review |
| Review | Model confidence below 0.6, a possible duplicate, a competitor's domain, or a message that tries to instruct the scorer | Waits for a person |

Score = 0.6 × fit + 0.4 × intent. Hot from 75 (and a Slack alert for sales leads), warm from 50.

## Evals

`samples/leads.json` holds 24 labeled leads: booked demos, pricing requests, an RFP for 1,800 vehicles, an
HVAC firm with 18 vans, a one-van courier, a bakery, a student, a job seeker, an SEO pitch, an outsourcing
pitch, a telematics partner, a reseller, an existing customer with a bug and the same customer expanding,
a competitor, a duplicate under another address, a prompt injection, a message in Spanish, a booking from a
personal address, a site whose robots.txt blocks crawlers, a throwaway address, a honeypot hit and a
two-word message. Each lists the routes, tiers and review outcome it may end with. A script scores every lead
on the real model with the same prompt, schema and rules as the app, and grades the final decision in code;
no model judges another.

```bash
LLM_API_KEY=<Gemini API key> uv run python -m scripts.eval   # writes evals/latest.json
```

**24 of 24 passed** on `gemini-3.5-flash-lite` (Gemini API free tier, 2026-11-03; results in
`evals/latest.json`):

| Measure | Result |
| --- | --- |
| Leads ending with an allowed route, tier and review outcome | 24 / 24 |
| Model calls | 22 (the honeypot hit and the throwaway address never reach the model) |
| Schema failures or model errors | 0 |
| Tokens per scored lead | 776 on average |
| Cost per scored lead at list price | $0.00052 (free on the free tier) |
| Model call time | 1.3 s median, 18.9 s slowest |
| Sent to a person | 4: the prompt injection, the duplicate, the competitor (rules) and the two-word message (model confidence 0.40) |

The rules did not change a route on this set; the model already routed every lead acceptably. They capped one
lead's fit (a personal address with no company website), marked two leads as spam without a model call, and
sent three of the four reviewed leads to a person.

## Measured so far

- **Tests:** 75 unit tests and 8 end-to-end tests pass, the end-to-end ones against the real n8n workflow.
- **Orchestration overhead,** on a Windows laptop with the local stand-ins for the model and HubSpot (20 form
  leads): n8n answered the webhook in 84 ms (median; slowest 417 ms), and each lead was synced 0.19 s after
  its webhook (median; slowest 0.64 s). Real runs add the model call and up to eight HubSpot calls.

## Staying free

| Service | Free allowance | How LeadLoom stays inside it |
| --- | --- | --- |
| Container Apps (consumption) | 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests a month | Both apps scale to zero; the API uses 0.25 vCPU and 0.5 GiB, n8n 0.5 vCPU and 1 GiB; the setup job runs once per deploy |
| Log Analytics | 5 GB of ingestion a month | Daily cap of 0.05 GB |
| Supabase | Free plan (two active projects, 500 MB database) | One database with two schemas; n8n prunes executions after 14 days |
| Gemini API | Free tier, rate-limited per day | At most 200 model calls a day; one call per lead; spam rules skip the model |
| HubSpot | Free developer test account | Up to eight API calls per lead |
| Slack, Postmark, Cal.com | Free plans | Only for the live lead sources |
| GitHub Actions and Container Registry | Free for public repositories | |

Azure's free PostgreSQL server (B1ms) allows one per subscription. ReplyPilot's Terraform creates it, so
LeadLoom never makes its own: it uses ReplyPilot's server when that is deployed, and Supabase otherwise.

## HubSpot and GoHighLevel

GoHighLevel has no free tier, so LeadLoom writes to HubSpot's free CRM. The HubSpot calls live in one module
(`app/hubspot.py`) and one function (`push` in `app/pipeline.py`); dedupe, scoring, review and the n8n
workflow do not depend on the CRM. A GoHighLevel version would map like this (not built or tested):

| LeadLoom step | HubSpot (built) | GoHighLevel (LeadConnector API v2) |
| --- | --- | --- |
| Find the contact by email | CRM search on `email` | Duplicate-contact search by email |
| Create or update the contact | Batch upsert by email | `POST /contacts/upsert` |
| Company | Company object, found by domain | Business object, or the contact's company name |
| Sales opportunity | Deal in the default pipeline (appointment scheduled or qualified to buy) | Opportunity in a pipeline stage |
| Explain the score | Note on the contact, company and deal | Note on the contact |
| Lead status | `hs_lead_status`, `lifecyclestage` | Tags and the opportunity's stage |
| Hot-lead alert | Slack from n8n | Slack from n8n, or a GoHighLevel workflow notification |
| Authentication | Service key (test account) | Private integration token for the sub-account |

## Tech stack

| Part | Choice |
| --- | --- |
| Workflow | n8n 2.45 (self-hosted): Webhook, HTTP Request, IF, Switch, Wait and Respond to Webhook nodes; workflow as committed JSON |
| Service | Python 3.12, FastAPI, Pydantic, httpx, psycopg 3 with a connection pool, Jinja2 pages with plain CSS |
| Model | OpenAI-compatible chat completions with a strict JSON schema (no SDK); Gemini API free tier by default |
| Data | PostgreSQL: one row per delivery, durable steps, an append-only audit log enforced by a trigger, advisory locks per lead |
| Integrations | HubSpot CRM v3/v4 (search, upsert, deals, notes, associations), Slack incoming webhook, Postmark inbound, Cal.com webhooks |
| Infrastructure | Terraform (azurerm 4): two Container Apps and a Container Apps job, Log Analytics, budget alert; Supabase or ReplyPilot's server for PostgreSQL |
| CI/CD | GitHub Actions: ruff, mypy, pytest, Playwright, Terraform validate, both image builds; deploy to GHCR and Container Apps over OpenID Connect |
| Tests | pytest against a real embedded PostgreSQL (pgserver) and mocks of the model, HubSpot and Slack; Playwright through the real n8n workflow |

## Run it locally

You need [uv](https://docs.astral.sh/uv/) and Node.js 24 (n8n 2.x requires it). No Docker, account or model
key is needed: an embedded PostgreSQL, a rule-based mock model and mocks of HubSpot and Slack stand in.

```bash
uv sync
npm install
uv run python -m scripts.dev   # PostgreSQL, the mocks, n8n and the API; logs in .data/
```

Then open http://localhost:8400 and send a sample. n8n is at http://localhost:5690 (`owner@leadloom.local`,
password `LeadLoom-local-1`); the first run downloads n8n through npx. To use a real model, set
`LLM_ENDPOINT`, `LLM_API_KEY` and `LLM_MODEL` in the shell that runs the script (see `.env.example`).

## Tests

```bash
uv run pytest       # 75 unit tests: intake and signatures, enrichment and SSRF guards, scoring rules,
                    # LLM schema and retries, dedupe, durable steps, HubSpot sync, review flow, eval grader
npm run test:e2e    # 8 Playwright tests: starts the whole local stack, n8n included
```

The end-to-end tests cover a hot booking that reaches HubSpot and Slack; an unsure lead that a reviewer edits
and approves, which resumes the waiting n8n run; a rejection; spam discarded without review; robots.txt
respected; a redelivered booking that maps to the same lead and a forged signature refused; demo leads
private to their browser; and a HubSpot outage that outlasts every retry, then a Retry that completes it.

## Security

- **Senders are verified** before anything is stored: Cal.com's HMAC-SHA256 over the raw body (timing-safe),
  Postmark's basic auth, and a honeypot field on the form. n8n and the API share a key, and n8n's retry
  webhook checks it too.
- **The model cannot widen its own power.** The lead's text goes in as JSON inside a data block, the answer
  must match a schema, and rules in code make the final call. A message that tries to instruct the scorer is
  flagged for a person.
- **Enrichment cannot be pointed at internal systems**: only http and https on standard ports, no private,
  loopback, link-local or reserved addresses on any redirect hop, and robots.txt is honoured.
- **Two audiences.** Live leads need the reviewer key; demo leads belong to one browser (an HttpOnly,
  SameSite cookie) and are deleted after 7 days.
- **The audit log cannot be edited**: a trigger rejects any `UPDATE` on it.
- **n8n's owner comes from the environment** (a bcrypt hash set by Terraform), so a fresh instance cannot be
  claimed by whoever opens it first.
- **Database traffic is encrypted.** On Azure's server the certificate is verified; Supabase's pooler uses
  Supabase's own CA, which LeadLoom does not pin yet.

## Limits and next steps

- The HubSpot calls have run only against a mock of the endpoints LeadLoom uses, not a real HubSpot account.
  LeadLoom is not deployed yet, so cold-start times on Container Apps are not measured.
- The eval set is small (24 leads) and the model passes all of it, so it shows that the prompt, schema and
  rules hold together rather than ranking models; harder, ambiguous leads would make it discriminate.
- Dedupe sees the deals LeadLoom created, not open deals made by hand in HubSpot; a deal search through
  associations would close that.
- Enrichment reads the home page only. The address check runs before each request, so a DNS answer that
  changes in between (rebinding) is not covered.
- Python's `robotparser` applies the first matching rule rather than RFC 9309's longest match.
- One reviewer key instead of accounts; sign-in with roles would come first in a team setting.

Deployment steps are in [DEPLOYMENT.md](DEPLOYMENT.md).
