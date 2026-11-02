-- LeadLoom's tables live in their own schema: on Supabase, `public` is exposed
-- through the Data API, and n8n keeps its tables in the `n8n` schema of the same
-- database. Every statement is idempotent; the app runs this file at startup.
create schema if not exists leadloom;
-- n8n does not create its own schema; doing it here lets a fresh database work
-- once the API has started (the deploy wakes the API before n8n's setup job).
create schema if not exists n8n;
set search_path = leadloom;

create table if not exists leads (
  id uuid primary key default gen_random_uuid(),
  source text not null check (source in ('form', 'email', 'booking')),
  external_id text not null,
  -- Demo leads belong to one browser; live leads have no workspace.
  workspace text,
  status text not null default 'received' check (status in (
    'received', 'qualified', 'awaiting_review', 'approved', 'rejected', 'synced', 'discarded', 'failed')),
  email text not null,
  lead jsonb not null,
  decision jsonb,
  resume_url text,
  hubspot jsonb,
  alert_claimed_at timestamptz,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- A redelivered webhook maps to the same lead.
  unique (source, external_id)
);
create index if not exists leads_email on leads (email);
create index if not exists leads_workspace on leads (workspace, created_at desc);

-- Durable steps: a retried qualification or sync replays finished steps from
-- here instead of calling the model or HubSpot again.
create table if not exists steps (
  lead_id uuid not null references leads on delete cascade,
  name text not null,
  output jsonb,
  tokens_in integer not null default 0,
  tokens_out integer not null default 0,
  cost_usd numeric(12, 6) not null default 0,
  ms integer not null default 0,
  created_at timestamptz not null default now(),
  primary key (lead_id, name)
);

create table if not exists audit (
  id bigserial primary key,
  lead_id uuid not null references leads on delete cascade,
  at timestamptz not null default now(),
  actor text not null,
  action text not null,
  detail jsonb not null default '{}'
);
create index if not exists audit_lead on audit (lead_id, id);

-- The audit log is append-only. Rows go only when their lead is deleted
-- (demo leads after 7 days).
create or replace function audit_append_only() returns trigger language plpgsql as $$
begin
  raise exception 'the audit log is append-only';
end $$;
drop trigger if exists audit_append_only on audit;
create trigger audit_append_only before update on audit for each row execute function audit_append_only();

-- Company websites already read, so a second lead from the same company does
-- not fetch the site again within a week.
create table if not exists sites (
  url text primary key,
  data jsonb not null,
  fetched_at timestamptz not null default now()
);
