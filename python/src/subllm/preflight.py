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
    def __init__(
        self,
        allowed_regions: set[str] | None = None,
        blocked_regions: set[str] | None = None,
        lookup: Callable[[], str] = default_lookup,
        ttl_s: float = 300.0,
        on_lookup_failure: str = "block",  # "block" | "allow"
    ) -> None:
        if on_lookup_failure not in ("block", "allow"):
            raise ValueError("on_lookup_failure must be 'block' or 'allow'")
        if bool(allowed_regions) == bool(blocked_regions):
            raise ValueError(
                "specify exactly one of allowed_regions / blocked_regions"
            )
        self._allowed = {r.upper() for r in allowed_regions} if allowed_regions else None
        self._blocked = {r.upper() for r in blocked_regions} if blocked_regions else None
        self._lookup = lookup
        self._ttl_s = ttl_s
        self._on_lookup_failure = on_lookup_failure
        self._cached_country: str | None = None
        self._cached_at: float = 0.0

    def _resolve_country(self) -> str:
        now = time.monotonic()
        if self._cached_country is not None and (now - self._cached_at) < self._ttl_s:
            return self._cached_country
        country = self._lookup().upper()
        self._cached_country = country
        self._cached_at = now
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
