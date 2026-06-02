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


def test_result_is_cached_within_ttl():
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return "US"

    guard = RegionGuard(allowed_regions={"US"}, lookup=counting, ttl_s=999)
    guard.check()
    guard.check()
    assert calls["n"] == 1  # second check served from cache
