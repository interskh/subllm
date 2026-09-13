"""Optional region/IP preflight. Off unless a client is given a RegionGuard.

Guards against VPN-off -> out-of-region requests that could fail or risk the
subscription account.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable

from subllm.errors import RegionError


def default_lookup(timeout_s: float = 5.0) -> str:
    """Resolve the current public IP's ISO country code via a small geo-IP HTTP
    service. Injectable so tests/consumers can replace it."""
    with urllib.request.urlopen("https://ipinfo.io/json", timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode())
    country = data.get("country")
    if not country:
        raise RuntimeError("geo-IP lookup returned no country")
    return country


class RegionGuard:
    """Blocks a call when the public IP is in the wrong place.

    Two failures wear the same `RegionError` and must not be treated alike. A
    region VIOLATION is a hard stop — the IP is where it is, and retrying cannot
    move it. A lookup FAILURE is transient: the geo-IP service timed out, reset
    the connection, or rate-limited us, and the very next attempt may succeed.
    Only the second is retried, and `check()` reports which one it raised.
    """

    def __init__(
        self,
        allowed_regions: set[str] | None = None,
        blocked_regions: set[str] | None = None,
        lookup: Callable[[], str] = default_lookup,
        ttl_s: float = 300.0,
        on_lookup_failure: str = "block",  # "block" | "allow"
        lookup_attempts: int = 3,
        lookup_backoff_s: float = 0.5,
        failure_ttl_s: float = 60.0,
    ) -> None:
        if on_lookup_failure not in ("block", "allow"):
            raise ValueError("on_lookup_failure must be 'block' or 'allow'")
        if bool(allowed_regions) == bool(blocked_regions):
            raise ValueError(
                "specify exactly one of allowed_regions / blocked_regions"
            )
        if lookup_attempts < 1:
            raise ValueError("lookup_attempts must be at least 1")
        self._allowed = {r.upper() for r in allowed_regions} if allowed_regions else None
        self._blocked = {r.upper() for r in blocked_regions} if blocked_regions else None
        self._lookup = lookup
        self._ttl_s = ttl_s
        self._on_lookup_failure = on_lookup_failure
        self._attempts = lookup_attempts
        self._backoff_s = lookup_backoff_s
        self._failure_ttl_s = failure_ttl_s
        self._cached_country: str | None = None
        self._cached_at: float = 0.0
        self._cached_failure: Exception | None = None
        self._failed_at: float = 0.0

    def _lookup_with_retry(self) -> str:
        """Exponential backoff over the injected lookup (base, 2*base, ...).

        Retries ANY exception: the lookup is an arbitrary callable reaching an
        arbitrary service, so there is no exception set worth enumerating, and
        the cost of a wrong retry here is one extra HTTP request.
        """
        last: Exception | None = None
        for i in range(self._attempts):
            try:
                return self._lookup().upper()
            except Exception as e:  # noqa: BLE001 — see docstring
                last = e
                if i < self._attempts - 1 and self._backoff_s:
                    time.sleep(self._backoff_s * (2 ** i))
        assert last is not None
        raise last

    def _resolve_country(self) -> str:
        """The cached country, or a fresh lookup.

        FAILURES are cached too, for their own shorter TTL. Without that, a
        caller that checks once per request — which is the normal shape, since
        the guard sits in front of every model call — turns one unreachable
        service into one HTTP request per request. Measured in wechat-digest:
        29 groups x 7 days per fire, hourly, is ~4,900 geo-IP requests a day
        against a free tier of 1,000, so the stampede manufactures the very
        429 it is reacting to. Re-raising the cached failure keeps the block
        (the country is still unknown) while letting the service recover.
        """
        now = time.monotonic()
        if self._cached_country is not None and (now - self._cached_at) < self._ttl_s:
            return self._cached_country
        if (
            self._cached_failure is not None
            and (now - self._failed_at) < self._failure_ttl_s
        ):
            raise self._cached_failure
        try:
            country = self._lookup_with_retry()
        except Exception as e:  # noqa: BLE001 — cached, then re-raised as-is
            self._cached_failure = e
            self._failed_at = time.monotonic()
            raise
        self._cached_country = country
        self._cached_at = now
        # A success clears the negative cache, so recovery is immediate rather
        # than waiting out failure_ttl_s.
        self._cached_failure = None
        return country

    def check(self) -> None:
        """Raise RegionError if the public IP violates the configured mode
        (whitelist: not in allowed_regions; blacklist: in blocked_regions), or if
        the lookup fails under on_lookup_failure='block'. No-op otherwise."""
        try:
            country = self._resolve_country()
        except Exception as e:  # noqa: BLE001 — lookup failure policy applies
            if self._on_lookup_failure == "allow":
                return
            raise RegionError(f"region lookup failed ({e}); blocking call") from e
        if self._allowed is not None:
            if country not in self._allowed:
                raise RegionError(
                    f"public IP in {country}; expected one of "
                    f"{sorted(self._allowed)} — is your VPN connected?"
                )
        elif country in self._blocked:
            raise RegionError(
                f"public IP in {country}, a blocked region — is your VPN connected?"
            )
