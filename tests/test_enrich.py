import httpx
import pytest

from app import enrich

PAGE = """<html><head><title> Acme  Fleet </title><meta name="description" content="Vans for hire">
<meta property="og:site_name" content="Acme"><style>.x{}</style></head>
<body><script>var secret = 1;</script><h1>We move things</h1><p>120 vans &amp; trucks</p></body></html>"""
# Literal public addresses resolve to themselves, so no DNS is needed.
SITE = "http://93.184.215.14"


def transport(routes: dict, seen: list | None = None) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        status, headers, body = routes.get(str(request.url), (404, {}, ""))
        return httpx.Response(status, headers=headers, text=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def html(body: str = PAGE):
    return 200, {"content-type": "text/html; charset=utf-8"}, body


def test_parse_html_reads_title_meta_and_visible_text():
    data = enrich.parse_html(PAGE)
    assert data == {
        "title": "Acme Fleet",
        "description": "Vans for hire",
        "site_name": "Acme",
        "text": "We move things 120 vans & trucks",
    }


def test_reads_the_page_when_robots_txt_is_missing():
    client = transport({f"{SITE}/": html()})
    data = enrich.enrich(f"{SITE}/", client)
    assert data["status"] == "ok" and data["title"] == "Acme Fleet"


def test_respects_robots_txt():
    seen: list = []
    robots = (200, {"content-type": "text/plain"}, "User-agent: LeadLoomBot\nDisallow: /\n")
    client = transport({f"{SITE}/robots.txt": robots, f"{SITE}/": html()}, seen)
    assert enrich.enrich(f"{SITE}/", client)["status"] == "robots_disallowed"
    assert seen == [f"{SITE}/robots.txt"]


def test_unreadable_robots_txt_means_no_fetch():
    client = transport({f"{SITE}/robots.txt": (503, {}, ""), f"{SITE}/": html()})
    assert enrich.enrich(f"{SITE}/", client)["status"] == "robots_unreachable"


def test_redirect_to_a_private_address_is_blocked():
    client = transport({f"{SITE}/": (302, {"location": "http://169.254.169.254/latest/meta-data"}, "")})
    data = enrich.enrich(f"{SITE}/", client)
    assert data["status"] == "blocked" and "private" in data["error"]


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/", "http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd", f"{SITE}:2375/"]
)
def test_private_hosts_and_odd_schemes_or_ports_are_blocked(url):
    assert enrich.enrich(url, transport({}))["status"] == "blocked"


def test_large_pages_are_cut_off():
    client = transport({f"{SITE}/": html("<title>Big</title>" + "<p>x</p>" * 200_000)})
    data = enrich.enrich(f"{SITE}/", client)
    assert data["status"] == "ok" and data["title"] == "Big" and len(data["text"]) <= 2500


def test_unknown_hosts_are_unreachable_not_blocked():
    data = enrich.enrich("https://no-such-company.invalid", transport({}))
    assert data["status"] == "unreachable" and "does not resolve" in data["error"]


def test_timeouts_and_non_html_are_reported():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert (
        enrich.enrich(f"{SITE}/", httpx.Client(transport=httpx.MockTransport(slow)))["status"] == "robots_unreachable"
    )
    client = transport({f"{SITE}/": (200, {"content-type": "application/pdf"}, "%PDF")})
    assert enrich.enrich(f"{SITE}/", client)["status"] == "not_html"
