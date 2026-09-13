import pytest
from subllm.preflight import RegionGuard
from subllm.errors import RegionError


def test_in_region_passes():
    guard = RegionGuard(allowed_regions={"US", "JP"}, lookup=lambda: "US", ttl_s=0)
    guard.check()  # no raise


def test_out_of_region_raises_and_names_country():
    guard = RegionGuard(allowed_regions={"US"}, lookup=lambda: "CN", ttl_s=0)
    with pytest.raises(RegionError) as ei:
        guard.check()
    assert "CN" in str(ei.value)


def test_lookup_failure_blocks_by_default():
    def boom():
        raise OSError("network down")

    guard = RegionGuard(allowed_regions={"US"}, lookup=boom, ttl_s=0)
    with pytest.raises(RegionError):
        guard.check()


def test_lookup_failure_can_allow():
    def boom():
        raise OSError("network down")

    guard = RegionGuard(
        allowed_regions={"US"}, lookup=boom, ttl_s=0, on_lookup_failure="allow"
    )
    guard.check()  # no raise


def test_blacklist_allows_any_unblocked_country():
    # Threat model B: tunneling out of a banned home region. Any VPN exit that
    # isn't the blocked region must pass without being enumerated up front.
    guard = RegionGuard(blocked_regions={"CN"}, lookup=lambda: "US", ttl_s=0)
    guard.check()  # no raise


def test_blacklist_blocks_blocked_country():
    guard = RegionGuard(blocked_regions={"CN", "RU"}, lookup=lambda: "CN", ttl_s=0)
    with pytest.raises(RegionError) as ei:
        guard.check()
    assert "CN" in str(ei.value)
    assert "blocked" in str(ei.value)


def test_blacklist_lookup_failure_blocks_by_default():
    # Can't confirm we're outside the banned region -> don't fire.
    def boom():
        raise OSError("network down")

    guard = RegionGuard(blocked_regions={"CN"}, lookup=boom, ttl_s=0)
    with pytest.raises(RegionError):
        guard.check()


def test_requires_exactly_one_mode():
    with pytest.raises(ValueError):
        RegionGuard(allowed_regions={"US"}, blocked_regions={"CN"})
    with pytest.raises(ValueError):
        RegionGuard()


def test_result_is_cached_within_ttl():
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return "US"

    guard = RegionGuard(allowed_regions={"US"}, lookup=counting, ttl_s=999)
    guard.check()
    guard.check()
    assert calls["n"] == 1  # second check served from cache


# ── lookup failure is transient; a region violation is not ──────────────────
# The guard sits in front of every model call, so these two behaviors are what
# decide whether an unreachable geo-IP service costs one request or hundreds.


def test_a_failing_lookup_is_retried_before_it_blocks():
    """A timeout or a reset connection may succeed on the very next attempt.
    Retrying is the whole difference between a transient blip and a lost run."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("connection reset")
        return "US"

    guard = RegionGuard(
        blocked_regions={"CN"}, lookup=flaky, ttl_s=0, lookup_backoff_s=0
    )
    guard.check()  # no raise
    assert calls["n"] == 3


def test_lookup_retries_are_bounded_and_then_block():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise OSError("network down")

    guard = RegionGuard(
        blocked_regions={"CN"}, lookup=boom, ttl_s=0,
        lookup_attempts=2, lookup_backoff_s=0, failure_ttl_s=0,
    )
    with pytest.raises(RegionError):
        guard.check()
    assert calls["n"] == 2


def test_a_region_violation_is_never_retried():
    """The IP is where it is. Retrying cannot move it, and each extra attempt is
    a pointless request against a service that rate-limits."""
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return "CN"

    guard = RegionGuard(
        blocked_regions={"CN"}, lookup=counting, ttl_s=0, lookup_backoff_s=0
    )
    with pytest.raises(RegionError) as ei:
        guard.check()
    assert calls["n"] == 1
    assert "blocked" in str(ei.value)


def test_a_failed_lookup_is_cached_so_callers_do_not_stampede():
    """The bug this closes. Successes were cached and failures were not, so a
    caller checking once per model call turned ONE unreachable service into one
    HTTP request per call — measured at ~4,900/day against a 1,000/day free
    tier, which manufactured the 429 that caused the failures."""
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise OSError("network down")

    guard = RegionGuard(
        blocked_regions={"CN"}, lookup=boom, ttl_s=0,
        lookup_attempts=1, lookup_backoff_s=0, failure_ttl_s=999,
    )
    for _ in range(10):
        with pytest.raises(RegionError):
            guard.check()
    assert calls["n"] == 1, "every check re-queried a service already known down"


def test_the_negative_cache_expires_so_recovery_is_possible():
    """Caching the failure must not become its own outage: the point is to stop
    the stampede, not to stop trying."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("network down")
        return "US"

    guard = RegionGuard(
        blocked_regions={"CN"}, lookup=flaky, ttl_s=0,
        lookup_attempts=1, lookup_backoff_s=0, failure_ttl_s=0,
    )
    with pytest.raises(RegionError):
        guard.check()
    guard.check()  # recovered on the next check, no raise
    assert calls["n"] == 2


def test_a_success_clears_a_cached_failure():
    """Otherwise a stale failure could outlive the recovery that disproved it."""
    seq = ["boom", "US", "boom"]
    calls = {"n": 0}

    def scripted():
        v = seq[calls["n"]]
        calls["n"] += 1
        if v == "boom":
            raise OSError("network down")
        return v

    guard = RegionGuard(
        blocked_regions={"CN"}, lookup=scripted, ttl_s=0,
        lookup_attempts=1, lookup_backoff_s=0, failure_ttl_s=0,
    )
    with pytest.raises(RegionError):
        guard.check()
    guard.check()
    assert guard._cached_failure is None


def test_allow_policy_still_short_circuits_a_cached_failure():
    """`on_lookup_failure='allow'` must not start blocking just because the
    failure now arrives from the cache rather than from a fresh lookup."""
    def boom():
        raise OSError("network down")

    guard = RegionGuard(
        blocked_regions={"CN"}, lookup=boom, ttl_s=0,
        lookup_attempts=1, lookup_backoff_s=0, failure_ttl_s=999,
        on_lookup_failure="allow",
    )
    guard.check()  # no raise
    guard.check()  # served from the negative cache, still no raise


def test_lookup_attempts_must_be_at_least_one():
    with pytest.raises(ValueError):
        RegionGuard(blocked_regions={"CN"}, lookup_attempts=0)
