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
