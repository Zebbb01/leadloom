"""Reads a lead's company website: robots.txt first, then the home page, with tight
timeouts, a size cap and no requests to private networks (the URL comes from the
lead, so it is untrusted)."""

import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "LeadLoomBot/0.1 (+https://github.com/Zebbb01/leadloom)"
ROBOTS_AGENT = "LeadLoomBot"
MAX_BYTES = 512_000
MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(6.0, connect=3.0)


class BlockedUrl(Exception):
    pass


class NoSuchHost(Exception):
    pass


class _Page(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.text: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = dict(attrs)
            key = (a.get("name") or a.get("property") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"] or "")
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())


def parse_html(html: str) -> dict:
    p = _Page()
    p.feed(html)
    p.close()
    clean = lambda s: " ".join(s.split())  # noqa: E731
    return {
        "title": clean(p.title)[:200],
        "description": clean(p.meta.get("description") or p.meta.get("og:description", ""))[:500],
        "site_name": clean(p.meta.get("og:site_name", ""))[:200],
        "text": clean(" ".join(p.text))[:2500],
    }


def check_host(host: str, allow_private: bool = False) -> None:
    # ponytail: checks the addresses once; a DNS answer that changes before httpx
    # connects (rebinding) is not covered. Pin the checked IP in a custom transport
    # if this ever fetches untrusted URLs at volume.
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        raise NoSuchHost(f"{host} does not resolve.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global and not allow_private:
            raise BlockedUrl(f"{host} points to a private or reserved address.")


def _get(client: httpx.Client, url: str, allow_private: bool) -> httpx.Response:
    """GET that follows up to 3 redirects, checking every hop, and reads at most MAX_BYTES."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BlockedUrl(f"{url} is not an http(s) URL.")
        if parts.port not in (None, 80, 443) and not allow_private:
            raise BlockedUrl(f"{url} uses a non-standard port.")
        check_host(parts.hostname, allow_private)
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as r:
            if r.is_redirect and "location" in r.headers:
                url = urljoin(url, r.headers["location"])
                continue
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
            # Decoded already, so only the content type travels with the body.
            headers = {"content-type": r.headers.get("content-type", "")}
            return httpx.Response(r.status_code, headers=headers, content=body[:MAX_BYTES], request=r.request)
    raise BlockedUrl("Too many redirects.")


def robots_allows(client: httpx.Client, url: str, allow_private: bool) -> bool | None:
    """True or False from robots.txt; None when robots.txt could not be read, which
    RFC 9309 says to treat as "disallow everything". A missing file (4xx) allows all."""
    parts = urlsplit(url)
    try:
        r = _get(client, f"{parts.scheme}://{parts.netloc}/robots.txt", allow_private)
    except httpx.HTTPError:
        return None
    if 400 <= r.status_code < 500:
        return True
    if r.status_code >= 300:
        return None
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(ROBOTS_AGENT, url)


def enrich(website: str, client: httpx.Client, allow_private: bool = False) -> dict:
    url = website.strip()
    if "://" not in url:
        url = f"https://{url}"
    try:
        allowed = robots_allows(client, url, allow_private)
        if allowed is None:
            return {"status": "robots_unreachable", "url": url}
        if not allowed:
            return {"status": "robots_disallowed", "url": url}
        r = _get(client, url, allow_private)
    except BlockedUrl as e:
        return {"status": "blocked", "url": url, "error": str(e)}
    except NoSuchHost as e:
        return {"status": "unreachable", "url": url, "error": str(e)}
    except httpx.HTTPError as e:
        return {"status": "unreachable", "url": url, "error": type(e).__name__}
    if r.status_code != 200:
        return {"status": "unreachable", "url": url, "error": f"HTTP {r.status_code}"}
    if "html" not in r.headers.get("content-type", ""):
        return {"status": "not_html", "url": url}
    return {"status": "ok", "url": str(r.request.url), **parse_html(r.text)}
