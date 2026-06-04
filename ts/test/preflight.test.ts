import { describe, it, expect } from "vitest";
import { RegionGuard } from "../src/preflight.js";
import { RegionError } from "../src/errors.js";

const boom = async (): Promise<string> => {
  throw new Error("network down");
};

describe("RegionGuard", () => {
  it("whitelist: in-region passes", async () => {
    const guard = new RegionGuard({
      allowedRegions: ["US", "JP"],
      lookup: async () => "US",
      ttlMs: 0,
    });
    await guard.check(); // no throw
  });

  it("whitelist: out-of-region throws RegionError naming the country", async () => {
    const guard = new RegionGuard({
      allowedRegions: ["US"],
      lookup: async () => "CN",
      ttlMs: 0,
    });
    await expect(guard.check()).rejects.toBeInstanceOf(RegionError);
    await expect(guard.check()).rejects.toThrow("CN");
  });

  it("blacklist: any unblocked country passes without enumerating exits", async () => {
    // Threat model B: tunneling out of a banned home region.
    const guard = new RegionGuard({
      blockedRegions: ["CN"],
      lookup: async () => "US",
      ttlMs: 0,
    });
    await guard.check(); // no throw
  });

  it("blacklist: a blocked country throws RegionError naming it", async () => {
    const guard = new RegionGuard({
      blockedRegions: ["CN", "RU"],
      lookup: async () => "CN",
      ttlMs: 0,
    });
    await expect(guard.check()).rejects.toBeInstanceOf(RegionError);
    await expect(guard.check()).rejects.toThrow(/CN.*blocked|blocked/);
  });

  it("lookup failure blocks by default in both modes", async () => {
    const wl = new RegionGuard({ allowedRegions: ["US"], lookup: boom, ttlMs: 0 });
    await expect(wl.check()).rejects.toBeInstanceOf(RegionError);
    const bl = new RegionGuard({ blockedRegions: ["CN"], lookup: boom, ttlMs: 0 });
    await expect(bl.check()).rejects.toBeInstanceOf(RegionError);
  });

  it("lookup failure can allow", async () => {
    const guard = new RegionGuard({
      allowedRegions: ["US"],
      lookup: boom,
      ttlMs: 0,
      onLookupFailure: "allow",
    });
    await guard.check(); // no throw
  });

  it("requires exactly one of allowedRegions / blockedRegions", () => {
    expect(
      () => new RegionGuard({ allowedRegions: ["US"], blockedRegions: ["CN"] }),
    ).toThrow();
    expect(() => new RegionGuard({})).toThrow();
  });

  it("caches the resolved country within ttl", async () => {
    let n = 0;
    const counting = async (): Promise<string> => {
      n += 1;
      return "US";
    };
    const guard = new RegionGuard({
      allowedRegions: ["US"],
      lookup: counting,
      ttlMs: 999_000,
    });
    await guard.check();
    await guard.check();
    expect(n).toBe(1); // second check served from cache
  });
});
