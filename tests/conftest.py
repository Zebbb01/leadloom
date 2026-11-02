"""Tests run against a real PostgreSQL (pgserver's embedded binaries, or
TEST_DATABASE_URL) and the mock services from scripts/mock_services.py."""

import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as database
from app.config import Settings
from scripts.mock_services import create_mock

SITES = Path(__file__).resolve().parent.parent / "samples" / "sites"


@pytest.fixture(scope="session")
def database_url(tmp_path_factory):
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    import pgserver

    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield server.get_uri()


@pytest.fixture
def db(database_url):
    pool = database.connect(database_url)
    with pool.connection() as c:
        c.execute("truncate leads, sites cascade")
    yield pool
    pool.close()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("app.http.time.sleep", lambda s: None)


class Router(httpx.BaseTransport):
    """The network for tests: http://mock/... goes to the mock services, http://sites.test
    serves the sample websites, and every other host gets a canned reply. Every request
    is recorded."""

    def __init__(self):
        self.mock = TestClient(create_mock())
        self.calls: list[httpx.Request] = []
        self.replies: dict[str, tuple[int, object]] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        body = request.read()
        if request.url.host == "mock":
            r = self.mock.request(request.method, str(request.url), headers=dict(request.headers), content=body)
            return httpx.Response(r.status_code, headers=r.headers, content=r.content)
        if request.url.host == "sites.test":
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
            page = SITES / f"{request.url.path.strip('/')}.html"
            if page.is_file():
                return httpx.Response(200, headers={"content-type": "text/html"}, content=page.read_bytes())
            return httpx.Response(404)
        status, reply = self.replies.get(request.url.host, (200, {"ok": True}))
        return httpx.Response(status, json=reply)

    def to(self, host: str, path: str = "") -> list[httpx.Request]:
        return [c for c in self.calls if c.url.host == host and c.url.path.startswith(path)]

    def hubspot(self) -> dict:
        return self.mock.get("/hubspot/_state").json()


@pytest.fixture
def router(monkeypatch):
    # Enrichment resolves hosts to refuse private networks; the test hosts do not exist.
    monkeypatch.setattr("app.enrich.check_host", lambda host, allow_private=False: None)
    return Router()


def make_settings(database_url: str, **overrides) -> Settings:
    values = {
        "database_url": database_url,
        "api_key": "test-key",
        "reviewer_key": "test-reviewer-key",
        "public_url": "http://testserver",
        "n8n_webhook_url": "http://n8n.test/webhook",
        "llm_endpoint": "http://mock/llm",
        "llm_api_key": "test",
        "hubspot_token": "test",
        "hubspot_base_url": "http://mock/hubspot",
        "hubspot_portal_id": "4242",
        "cal_webhook_secret": "cal-secret",
        "inbound_email_secret": "mail-secret",
        **overrides,
    }
    return Settings(**values)


@pytest.fixture
def settings(database_url):
    return make_settings(database_url)


@pytest.fixture
def clients(router):
    """The outbound clients the app would make, all on the test network."""
    from app import hubspot

    return {
        "web": httpx.Client(transport=router),
        "model": httpx.Client(transport=router),
        "crm": hubspot.client("test", "http://mock/hubspot", router),
        "n8n": httpx.Client(transport=router),
    }
