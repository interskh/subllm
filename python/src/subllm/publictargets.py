"""D-4b: judge whether a fetch target is a public web address.

"Public" is judged on where a connection actually goes, not on how the URL
reads: a public-looking name can resolve to loopback. A hostname that cannot be
resolved is NOT treated as public — unknown evidence fails closed.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import urlsplit

Resolver = Callable[[str], list[str]]

_PUBLIC_SCHEMES = ("http", "https")

# All-digit / 0x-hex dotted forms: alternate spellings of an IP address that
# `ipaddress` refuses but the resolver may accept.
_NUMERIC_HOST = re.compile(
    r"[0-9.]+|0[xX][0-9a-fA-F]+(\.0[xX]?[0-9a-fA-F]+)*"
)


def default_resolver(host: str) -> list[str]:
    """Every address `host` resolves to, as strings."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _address_reason(text: str) -> str | None:
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None  # not a literal; caller resolves it
    # is_global alone is not enough: a multicast address reports is_global True
    # on IPv4, so the classes D-4b names are also checked by name.
    if addr.is_global and not (
        addr.is_multicast
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_private
        or addr.is_reserved
    ):
        return None
    return f"{text} is not a public address"


#: Machine-readable reason codes. Callers must not classify the prose: a
#: consumer that needs to treat "the name did not resolve" differently from
#: "the address is not public" (a transient DNS miss is a delivery failure, not
#: an isolation violation) reads the code.
CODE_MISSING = "missing"
CODE_SCHEME = "scheme"
#: Non-public BY INSPECTION: the URL itself names a private/loopback/reserved
#: address, an alternate IP spelling, or a loopback name.
CODE_NON_PUBLIC = "non_public"
#: Non-public BY RESOLUTION: an ordinary-looking name whose addresses are not
#: public. Split from the above because the two carry different evidence — a
#: sinkhole (Pi-hole answering 0.0.0.0) makes an innocent name resolve
#: non-public, while nothing legitimate dials a private literal.
CODE_RESOLVED_NON_PUBLIC = "resolved_non_public"
CODE_UNRESOLVED = "unresolved"


def classify_url(
    url: str | None,
    *,
    resolve: Resolver = default_resolver,
    unresolved_is_public: bool = False,
) -> str | None:
    """Return None when `url` is a public http/https target, else the reason it
    is not. A missing URL is not public (nothing to prove it is).

    `unresolved_is_public` relaxes ONLY the "name did not resolve" branch, for
    callers judging a target nothing connected to (a search result). Everything
    judged non-public by inspection still fails."""
    return classify_url_detail(
        url, resolve=resolve, unresolved_is_public=unresolved_is_public
    )[0]


def classify_url_detail(
    url: str | None,
    *,
    resolve: Resolver = default_resolver,
    unresolved_is_public: bool = False,
) -> tuple[str | None, str | None]:
    """`classify_url` plus the machine-readable code for the reason."""
    if not url:
        return "missing URL", CODE_MISSING
    parts = urlsplit(url)
    if parts.scheme.lower() not in _PUBLIC_SCHEMES:
        return (
            f"scheme {parts.scheme or '(none)'!r} is not http/https: {url!r}",
            CODE_SCHEME,
        )
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        return f"no host in {url!r}", CODE_MISSING
    # A host that is an IP literal is judged directly; ipaddress.is_global is
    # false for loopback, private, link-local, unspecified, multicast and the
    # other reserved ranges, which is exactly the class D-4b names.
    literal = _address_reason(host)
    if literal is not None:
        return f"{literal}: {url!r}", CODE_NON_PUBLIC
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None, None  # public literal
    # A numeric host that `ipaddress` rejects is an alternate IP spelling
    # (octal `0177.0.0.1`, hex `0x7f000001`, the bare integer form). getaddrinfo
    # resolves those platform-dependently — macOS reads `0177.0.0.1` as
    # 177.0.0.1, which looks public — so they are refused, not resolved.
    if _NUMERIC_HOST.fullmatch(host):
        return (
            f"host {host!r} is a non-decimal IP spelling: {url!r}",
            CODE_NON_PUBLIC,
        )
    if host == "localhost" or host.endswith(".localhost"):
        return f"host {host!r} is loopback: {url!r}", CODE_NON_PUBLIC
    try:
        addresses = resolve(host)
    except Exception as e:  # noqa: BLE001 — any resolution failure fails closed
        if unresolved_is_public:
            return None, None
        return (
            f"host {host!r} could not be resolved ({e}): {url!r}",
            CODE_UNRESOLVED,
        )
    if not addresses:
        if unresolved_is_public:
            return None, None
        return f"host {host!r} resolved to nothing: {url!r}", CODE_UNRESOLVED
    for text in addresses:
        if _address_reason(text) is not None:
            return (
                f"host {host!r} resolves to non-public address {text}: {url!r}",
                CODE_RESOLVED_NON_PUBLIC,
            )
    return None, None


class CachingResolver:
    """getaddrinfo once per host per call."""

    def __init__(self, resolve: Resolver = default_resolver) -> None:
        self._resolve = resolve
        self._cache: dict[str, list[str]] = {}

    def __call__(self, host: str) -> list[str]:
        if host not in self._cache:
            self._cache[host] = self._resolve(host)
        return self._cache[host]
