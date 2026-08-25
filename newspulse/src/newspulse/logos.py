"""Fetching a client's real logo, once, and keeping it locally.

A monogram is a decent fallback but it is not the company's mark, and a
portfolio of initials reads as unfinished. This fetches the real logo from the
company's own site when a client is created.

Two decisions worth stating:

**Fetched once, stored locally.** The image is inlined as a ``data:`` URI in the
client row rather than kept as a remote URL. A remote URL would mean an outbound
request on every page load — which re-exposes who you monitor to whoever hosts
the image, every time anyone opens the dashboard, and breaks the offline
guarantee (DEC-3). One fetch at creation, then the dashboard never leaves the
machine again.

**Only the company's own site is contacted.** No logo API, no third-party
resolver: the one request goes to the domain the operator entered, which is a
site the tool already monitors. Nothing about the portfolio is disclosed to a
service that did not already know.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import socket
import re
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger(__name__)

_USER_AGENT = "NewsPulse/0.1 (local media monitor; logo fetch)"
_TIMEOUT = 10

# A logo is small. Anything past this is a hero image or a mis-tagged asset, and
# inlining it would bloat every dashboard page that renders the client.
_MAX_BYTES = 400_000

# Only real image types, and only ones a browser renders inline from a data URI.
# SVG is excluded deliberately: it is executable markup, and inlining a remote
# SVG into the page would hand the source site script execution in this origin.
_ALLOWED_TYPES = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/webp": "image/webp",
    "image/x-icon": "image/x-icon",
    "image/vnd.microsoft.icon": "image/x-icon",
    "image/gif": "image/gif",
}

# Preferred first: a touch icon is a square, high-resolution mark designed to
# stand alone, which is exactly what a card needs. og:image is a fallback that
# often carries a banner rather than a logo, so it comes last before favicon.
_ICON_PATTERNS = (
    re.compile(r'<link[^>]+rel=["\'][^"\']*apple-touch-icon[^"\']*["\'][^>]*>', re.I),
    re.compile(r'<link[^>]+rel=["\'][^"\']*icon[^"\']*["\'][^>]*>', re.I),
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]*>', re.I),
)
_HREF_RE = re.compile(r'(?:href|content)=["\']([^"\']+)["\']', re.I)
_SIZES_RE = re.compile(r'sizes=["\'](\d+)x\d+["\']', re.I)


def normalize_website(raw: str | None) -> str | None:
    """A bare ``https://host`` for whatever the operator typed.

    Accepts "zalando.de", "www.zalando.de", or a full URL with a path, since an
    operator pasting from a browser bar should not have to think about it.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    parsed = urllib.parse.urlsplit(value)
    if not parsed.netloc:
        return None
    return f"https://{parsed.netloc}"


class Blocked(Exception):
    """A URL this fetcher will not follow."""


def _reachable(url: str) -> str:
    """``url`` if it names a public HTTP host, raising otherwise.

    Everything this module fetches is decided by somebody else's HTML. The
    operator types a company's website; the page's own ``<link rel="icon">`` says
    where the icon is, and ``urljoin`` hands an *absolute* href straight back, so
    a hostile or merely compromised site chooses the second request's target
    outright. Three things follow from that and each is a separate refusal:

    * Only http and https. ``urlopen`` honours ``file:`` and ``ftp:`` too, and
      Python synthesises a Content-Type from the extension, so
      ``file:///data/x.png`` was a local file read and inlined into the page.
    * No private, loopback, link-local or otherwise reserved address. The
      container can reach its own network and the platform's metadata service;
      this process has no business fetching a logo from either.
    * The name is resolved here rather than trusted. "localhost" and a domain
      whose A record points at 127.0.0.1 are the same request.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise Blocked(f"scheme {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise Blocked("no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise Blocked(f"unresolvable: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise Blocked(f"non-public address {address}")
    return url


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check every hop.

    A public host that answers 302 to ``http://169.254.169.254/`` would walk
    straight past a check that only looked at the URL we started with.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _reachable(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardedRedirects)


def _get(url: str, *, limit: int = _MAX_BYTES) -> tuple[bytes, str] | None:
    try:
        _reachable(url)
    except Blocked as exc:
        _log.info("logo fetch refused for %s: %s", url, exc)
        return None
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _OPENER.open(request, timeout=_TIMEOUT) as response:
            # read one byte past the cap so an oversized asset is detected rather
            # than silently truncated into a corrupt image.
            body = response.read(limit + 1)
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    except (urllib.error.URLError, OSError, ValueError, Blocked) as exc:
        _log.debug("logo fetch failed for %s: %s", url, exc)
        return None
    if len(body) > limit:
        return None
    return body, content_type


def _candidate_icons(website: str) -> list[str]:
    """Icon URLs advertised by the site's own HTML, best first."""
    page = _get(website, limit=1_000_000)
    if page is None:
        return []
    html = page[0].decode("utf-8", errors="replace")
    found: list[tuple[int, str]] = []
    for rank, pattern in enumerate(_ICON_PATTERNS):
        for tag in pattern.findall(html):
            href = _HREF_RE.search(tag)
            if not href:
                continue
            size = _SIZES_RE.search(tag)
            # Sort key: pattern rank first, then largest declared size — a 180px
            # touch icon beats a 16px favicon from the same rel group.
            found.append((rank * 10_000 - int(size.group(1)) if size else rank * 10_000,
                          urllib.parse.urljoin(website, href.group(1))))
    return [url for _, url in sorted(found)]


def fetch_logo(website: str | None) -> str | None:
    """A ``data:`` URI for this site's logo, or ``None`` if nothing usable.

    Never raises: a missing logo is cosmetic, and creating a client must not fail
    because a website was slow or the mark could not be found.
    """
    base = normalize_website(website)
    if base is None:
        return None

    for candidate in [*_candidate_icons(base), urllib.parse.urljoin(base, "/favicon.ico")]:
        result = _get(candidate)
        if result is None:
            continue
        body, content_type = result
        mime = _ALLOWED_TYPES.get(content_type)
        if mime is None or not body:
            continue
        encoded = base64.b64encode(body).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    return None


__all__ = ["fetch_logo", "normalize_website"]
