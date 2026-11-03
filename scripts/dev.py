"""Runs LeadLoom locally with no accounts: PostgreSQL (pgserver's embedded binaries),
the mock model/HubSpot/Slack, n8n with the committed workflow imported and published,
and the API. Ctrl+C stops everything.

    uv run python -m scripts.dev

n8n 2.x needs Node.js 24. It runs through `npx n8n@<version>` unless N8N_CMD names
another way to start it. Any variable already set in the environment wins over the
local defaults below, so LLM_ENDPOINT, LLM_API_KEY and LLM_MODEL switch to a real model."""

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".data"
N8N_VERSION = (ROOT / "n8n" / "Dockerfile").read_text(encoding="utf-8").split("n8nio/n8n:")[1].split()[0]
API_PORT, MOCK_PORT, N8N_PORT = 8400, 4041, 5690
MOCK = f"http://127.0.0.1:{MOCK_PORT}"
# n8n owner for local runs: owner@leadloom.local / LeadLoom-local-1
OWNER_HASH = "$2b$10$B7gEc0hroelY23lczWZNHOLFfoq4ks9qXyQrQdC6kN4A/AiJ.kFH6"
children: list[subprocess.Popen] = []


def start(cmd: list[str], env: dict, log: str) -> subprocess.Popen:
    out = open(DATA / log, "w", encoding="utf-8")  # noqa: SIM115 - closed with the process
    p = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=os.name != "nt"
    )
    children.append(p)
    return p


def wait_for(url: str, what: str, timeout: float = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        if any(p.poll() is not None for p in children):
            sys.exit(f"A process stopped while waiting for {what}; see the logs in .data/.")
        time.sleep(0.5)
    sys.exit(f"{what} did not start within {timeout:.0f} s; see the logs in .data/.")


def stop_all() -> None:
    for p in reversed(children):
        if p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                os.killpg(p.pid, 15)


def main() -> None:
    import pgserver

    DATA.mkdir(exist_ok=True)
    try:
        server = pgserver.get_server(DATA / "pg", cleanup_mode="stop")
    except subprocess.TimeoutExpired:
        # After an unclean stop, PostgreSQL on Windows spends ~30 s on recovery
        # (it waits on its own log file); pgserver gives up after 10 s but caches
        # the failed handle, so the retry builds a new one.
        time.sleep(40)
        server = pgserver.PostgresServer((DATA / "pg").resolve(), cleanup_mode="stop")
    database_url = server.get_uri()
    with psycopg.connect(database_url, autocommit=True) as c:
        c.execute("create schema if not exists n8n")
    db = urlsplit(database_url)
    defaults = {
        "DATABASE_URL": database_url,
        "LEADLOOM_API_KEY": "local-api-key",
        "REVIEWER_KEY": "local-reviewer-key",
        "PUBLIC_URL": f"http://localhost:{API_PORT}",
        "LEADLOOM_N8N_WEBHOOK_URL": f"http://localhost:{N8N_PORT}/webhook",
        "LLM_ENDPOINT": f"{MOCK}/llm",
        "LLM_API_KEY": "local",
        "LLM_MODEL": "mock",
        "HUBSPOT_TOKEN": "local",
        "HUBSPOT_BASE_URL": f"{MOCK}/hubspot",
        "CAL_WEBHOOK_SECRET": "local-cal-secret",
        "INBOUND_EMAIL_SECRET": "localmailsecret",
        "ENRICH_ALLOW_PRIVATE": "true",
        "DEMO_DAILY_PER_WORKSPACE": "1000",
        # n8n
        "DB_TYPE": "postgresdb",
        # On Linux pgserver listens on a Unix socket, named in the URL's host parameter.
        "DB_POSTGRESDB_HOST": parse_qs(db.query).get("host", [db.hostname or "127.0.0.1"])[0],
        "DB_POSTGRESDB_PORT": str(db.port or 5432),
        "DB_POSTGRESDB_DATABASE": db.path.lstrip("/"),
        "DB_POSTGRESDB_USER": db.username or "postgres",
        "DB_POSTGRESDB_PASSWORD": db.password or "",
        "DB_POSTGRESDB_SCHEMA": "n8n",
        "N8N_USER_FOLDER": str(DATA / "n8n"),
        "N8N_PORT": str(N8N_PORT),
        "N8N_RUNNERS_BROKER_PORT": str(N8N_PORT + 1),
        "WEBHOOK_URL": f"http://localhost:{N8N_PORT}/",
        "N8N_ENCRYPTION_KEY": "local-dev-encryption-key",
        "N8N_SECURE_COOKIE": "false",
        "N8N_INSTANCE_OWNER_MANAGED_BY_ENV": "true",
        "N8N_INSTANCE_OWNER_EMAIL": "owner@leadloom.local",
        "N8N_INSTANCE_OWNER_FIRST_NAME": "LeadLoom",
        "N8N_INSTANCE_OWNER_LAST_NAME": "Owner",
        "N8N_INSTANCE_OWNER_PASSWORD_HASH": OWNER_HASH,
        "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false",
        "N8N_DIAGNOSTICS_ENABLED": "false",
        "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
        "N8N_TEMPLATES_ENABLED": "false",
        "N8N_PERSONALIZATION_ENABLED": "false",
        "GENERIC_TIMEZONE": "UTC",
        "LEADLOOM_API_URL": f"http://127.0.0.1:{API_PORT}",
        "SLACK_WEBHOOK_URL": f"{MOCK}/slack/webhook",
    }
    env = {**defaults, **os.environ}
    n8n = shlex.split(env.get("N8N_CMD") or f"npx --yes n8n@{N8N_VERSION}", posix=os.name != "nt")
    if os.name == "nt" and n8n[0] == "npx":
        n8n[0] = "npx.cmd"
    try:
        start([sys.executable, "-m", "scripts.mock_services", str(MOCK_PORT)], env, "mock.log")
        wait_for(f"{MOCK}/health", "the mock services")
        with open(DATA / "n8n-setup.log", "w", encoding="utf-8") as log:
            # The same steps as n8n/setup.sh, which the deploy runs.
            for args in (
                ["unpublish:workflow", "--id=LeadLoomIntake01"],
                ["import:workflow", "--separate", "--input=n8n/workflows"],
                ["publish:workflow", "--id=LeadLoomIntake01"],
            ):
                print("n8n", *args, flush=True)
                # Unpublishing fails on a fresh database, where the workflow does not exist yet.
                check = args[0] != "unpublish:workflow"
                subprocess.run([*n8n, *args], cwd=ROOT, env=env, check=check, stdout=log, stderr=subprocess.STDOUT)
        start([*n8n, "start"], env, "n8n.log")
        wait_for(f"http://127.0.0.1:{N8N_PORT}/healthz/readiness", "n8n")
        start(
            [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(API_PORT)],
            env,
            "api.log",
        )
        wait_for(f"http://127.0.0.1:{API_PORT}/health", "the API")
        print(f"LeadLoom  http://localhost:{API_PORT}  (reviewer key: {env['REVIEWER_KEY']})")
        print(f"n8n       http://localhost:{N8N_PORT}  (owner@leadloom.local / LeadLoom-local-1)", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(1)
        sys.exit("A process stopped; see the logs in .data/.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"n8n {e.cmd[-2]} failed; see .data/n8n-setup.log.")
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()


if __name__ == "__main__":
    main()
