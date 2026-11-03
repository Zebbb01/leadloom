# Deploying LeadLoom to Azure (free tier)

Everything below stays within free tiers. Allow about 45 minutes, most of it creating accounts.

## What gets created

`infra/` is a Terraform configuration that creates one resource group in Southeast Asia with:

| Resource | Tier |
| --- | --- |
| Container Apps environment, unless an existing one is shared (see below) | Consumption (monthly free grant) |
| Container App `api` (FastAPI, 0 to 1 replica of 0.25 vCPU / 0.5 GiB) | Consumption |
| Container App `n8n` (0 to 1 replica of 0.5 vCPU / 1 GiB) | Consumption |
| Container Apps job `n8n-setup`, run once per deploy to import and publish the workflows | Consumption |
| Log Analytics workspace, with the environment | Pay-as-you-go with a 0.05 GB daily cap (under the 5 GB free) |
| A managed identity for GitHub Actions and its role assignment | Free |
| Optional: a 1-unit monthly budget that emails you at the first cent of cost | Free |

It does **not** create a database. LeadLoom and n8n share one PostgreSQL database, in the schemas `leadloom`
and `n8n`:

- **If ReplyPilot is deployed**, use its PostgreSQL Flexible Server (B1ms, free for 12 months). Azure's free
  allowance covers one such server per subscription, so a second server would be billed.
- **Otherwise**, use a free Supabase project. This is the default path below.

**Check first:**

- On a **Free Trial** subscription with the spending limit on, nothing can charge your card: when the $200
  credit or the trial runs out, Azure disables the subscription and the apps stop. Do not upgrade to
  pay-as-you-go to keep them running; run `terraform destroy` or let it lapse.
- The Container Apps and Log Analytics allowances are shared with any other app in the subscription (such as
  LedgerLens). All of them scale to zero, so together they stay well inside the grants.
- **A Free Trial subscription allows one Container Apps environment.** If another app already has it (LedgerLens
  does), add `-var 'shared_environment={name="cae-ledgerlens-xxxxxx", resource_group="rg-ledgerlens-xxxxxx"}'`
  to the apply and keep `location` in that environment's region (`az containerapp env list` shows both).
  LeadLoom's apps then join it, its logs go to that environment's workspace, and the GitHub deploy identity
  also gets Contributor on that environment so it can update its apps there.

## 1. Publish the images

1. Create the GitHub repository `Zebbb01/leadloom` and push `main` and `develop`. For another name, change
   `api_image` and `n8n_image` in `infra/variables.tf`.
2. The **Deploy** workflow builds two images on every push to `main` and pushes them to
   `ghcr.io/zebbb01/leadloom-api` and `ghcr.io/zebbb01/leadloom-n8n`. Its Azure step is skipped until step 6
   is done. If the workflow did not start on the push, run it from **Actions** → **Deploy** → **Run
   workflow**, or with `gh workflow run deploy.yml --ref main`.
3. Container Apps pulls the images without credentials, so both packages must be public: GitHub → your
   profile → **Packages** → each package → **Package settings** → **Change visibility** → **Public**.
4. Print the subject GitHub uses in this repository's OpenID Connect tokens; Terraform needs it:

   ```bash
   gh api repos/Zebbb01/leadloom/actions/oidc/customization/sub
   ```

   New repositories use immutable IDs, so the prefix looks like `repo:Zebbb01@179115543/leadloom@123456789`.

## 2. The database

**Supabase (default).** At [supabase.com](https://supabase.com), create a project in an organization on the
**Free** plan (a project in a paid organization is billed; the Free plan allows two active projects). Keep
the database and the apps in one part of the world, since every request makes several database round trips:
pick Singapore to match the default `location` (`southeastasia`). A project elsewhere still works, more
slowly. Then open **Connect** → **Session pooler** and note:

| Terraform variable | Value |
| --- | --- |
| `postgres_host` | the pooler host exactly as shown, such as `aws-0-ap-southeast-1.pooler.supabase.com` (newer projects use `aws-1-`) |
| `postgres_user` | `postgres.<project-ref>` |
| `postgres_password` | the database password you set for the project |

Use the session pooler, not the direct connection: the direct host is IPv6-only, and Container Apps connects
over IPv4. Leave `postgres_tls_verify` false: Supabase's pooler certificate comes from Supabase's own CA, so
the connection is encrypted but the certificate is not checked. A free project pauses after a week without
traffic; resume it from the dashboard.

**ReplyPilot's server (when deployed).** Create a database for LeadLoom on it and read the administrator
password from ReplyPilot's Terraform state:

```bash
az postgres flexible-server db create -g <replypilot-resource-group> -s <psql-server-name> -d leadloom
echo 'nonsensitive(random_password.db.result)' | terraform -chdir=../replypilot/infra console
```

Then use `postgres_host=<psql-server-name>.postgres.database.azure.com`, `postgres_user=replypilot`,
`postgres_database=leadloom` and `postgres_tls_verify=true`. n8n 2.45 logs that PostgreSQL 16 has
"compatibility support only"; it runs.

The API creates both schemas when it starts, and the deploy wakes the API before n8n's setup job runs.

## 3. Keys from the free services

**Model (required for AI scoring).** Create a key at [Google AI Studio](https://aistudio.google.com/apikey).
The Gemini API's free tier needs no card; leave billing off for the project so nothing beyond it can be
charged. On the free tier Google may use prompts to improve its products, so run only test data through it.
Without a key LeadLoom still works: every lead is scored by the rules and goes to a person.

**HubSpot (required for the CRM sync).** Create a free HubSpot account. Under **Development** → **Testing** →
**Test accounts**, create a developer test account, then open it. Under **Development** → **Keys** →
**Service keys**, create a key with the read and write scopes for contacts, companies and deals
(`crm.objects.contacts.read`, `crm.objects.contacts.write`, and the same for `companies` and `deals`; notes
use the contacts scope). The account ID in the account menu is `hubspot_portal_id`, which turns record IDs
into links. A test account expires after 90 days without API calls.

**Slack (optional, for hot-lead alerts).** In a free Slack workspace, create an app at
[api.slack.com/apps](https://api.slack.com/apps), turn on **Incoming Webhooks** and add one for a channel.
The URL is `slack_webhook_url`.

## 4. Create the Azure resources

From [Azure Cloud Shell](https://shell.azure.com) (Bash; Terraform and the Azure CLI are already there) or a
machine with both installed and `az login` done:

```bash
git clone https://github.com/Zebbb01/leadloom.git
cd leadloom/infra
terraform init
terraform apply \
  -var 'github_oidc_subject_prefix=repo:Zebbb01@179115543/leadloom@123456789' \
  -var 'postgres_host=aws-0-ap-southeast-1.pooler.supabase.com' \
  -var 'postgres_user=postgres.abcdefghijklmnop' \
  -var 'postgres_password=...' \
  -var 'llm_api_key=...' \
  -var 'hubspot_token=pat-na1-...' \
  -var 'hubspot_portal_id=12345678' \
  -var 'slack_webhook_url=https://hooks.slack.com/services/...' \
  -var 'n8n_owner_email=you@example.com' \
  -var 'alert_email=you@example.com'   -var 'shared_environment={name="cae-ledgerlens-xxxxxx", resource_group="rg-ledgerlens-xxxxxx"}'   # Free Trial: only if LedgerLens is deployed
```

Keep the Terraform state (`terraform.tfstate`): it holds the generated keys and is how you update or remove
the resources later. It is ignored by git.

## 5. Connect GitHub Actions

```bash
terraform output github_variables
```

Add each value as a repository **variable** (not a secret; none of them is sensitive): GitHub → **Settings** →
**Secrets and variables** → **Actions** → **Variables**. Then run the Deploy workflow once more
(`gh workflow run deploy.yml --ref main`). It rolls out both images and runs the setup job, which imports the
workflows into n8n's database and publishes the intake workflow. Every later push to `main` does the same.

## 6. Connect the lead sources

```bash
terraform output webhooks
```

**Website form.** Post JSON (or a normal form post) to `webhooks.form`. Include a hidden `nickname` field
that people never see; bots fill it in and the lead is marked as spam. A `submission_id` (a random ID made
when the page loads) keeps a double click from making two leads:

```html
<form id="contact">
  <input name="name" required> <input name="email" type="email" required>
  <input name="company"> <input name="website"> <textarea name="message"></textarea>
  <input name="nickname" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px">
  <button>Send</button>
</form>
<script>
  const id = crypto.randomUUID();
  document.querySelector("#contact").addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target));
    await fetch("https://<n8n host>/webhook/leads/form", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...data, submission_id: id }),
    });
  });
</script>
```

**Inbound email (Postmark, free developer plan).** Create a server, open its **Inbound** stream and set the
webhook URL to the value of `terraform output -raw postmark_inbound_url` (it carries the password as basic
auth). Email the inbound address Postmark shows, or forward a mailbox to it.

**Bookings (Cal.com, free plan).** Under **Settings** → **Developer** → **Webhooks**, add a webhook with
`webhooks.booking` as the subscriber URL, the **Booking Created** trigger, and the value of
`terraform output -raw cal_webhook_secret` as the secret. Optional booking questions named `company` and
`website` give the scorer more to work with.

Each source answers within a second once n8n is awake. After a quiet spell n8n takes up to about a minute to
wake; senders that retry (Postmark does) deliver anyway, and a redelivered webhook maps to the same lead.

## 7. Check it

```bash
terraform output api_url
terraform output -raw reviewer_key        # signs a reviewer in on the review page
terraform output n8n_url
terraform output -raw n8n_owner_password  # with n8n_owner_email, signs in to the n8n editor
```

Open the API's URL and send one of the samples. The first request after a quiet spell wakes the API (about
30 seconds) and then n8n. In n8n, **Executions** shows each run of the intake workflow.

To score the eval set on the real model and fill in the README:

```bash
LLM_API_KEY=... uv run python -m scripts.eval
```

## Logs

Open the Log Analytics workspace → **Logs**:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s endswith "-n8n" or ContainerAppName_s endswith "-api"
| project TimeGenerated, ContainerAppName_s, Log_s
| order by TimeGenerated desc
```

The per-lead story (webhook, qualification, review, sync, alert) is in each lead's audit log on the review page.

## Remove everything

```bash
cd leadloom/infra
terraform destroy
```

Before the free trial ends (about 2026-12-01), destroy the resources or let the subscription lapse, and
remove the live link from the README and the portfolio.
