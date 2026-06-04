/** Optional region/IP preflight. Off unless a client is given a RegionGuard.
 *
 *  Guards against a wrong-region public IP that could fail or risk the
 *  subscription account, in either of two mutually-exclusive modes:
 *  - whitelist (`allowedRegions`): block unless the IP's country is in the set;
 *  - blacklist (`blockedRegions`): block only if it is in the set (for users who
 *    tunnel out of an unsupported/banned home region and need not enumerate every
 *    acceptable VPN exit).
 */
import { performance } from "node:perf_hooks";
import { RegionError } from "./errors.js";

/** Resolve the current public IP's ISO country code via a small geo-IP HTTP
 *  service. Injectable so tests/consumers can replace it. */
export async function defaultLookup(timeoutMs = 5000): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch("https://ipinfo.io/json", {
      signal: controller.signal,
    });
    if (!resp.ok) throw new Error(`geo-IP lookup HTTP ${resp.status}`);
    const data = (await resp.json()) as { country?: string };
    if (!data.country) throw new Error("geo-IP lookup returned no country");
    return data.country;
  } finally {
    clearTimeout(timer);
  }
}

export type LookupFn = () => Promise<string>;
export type OnLookupFailure = "block" | "allow";

export interface RegionGuardOptions {
  allowedRegions?: Iterable<string>;
  blockedRegions?: Iterable<string>;
  lookup?: LookupFn;
  ttlMs?: number;
  onLookupFailure?: OnLookupFailure;
}

function toUpperSet(regions: Iterable<string>): Set<string> {
  return new Set([...regions].map((r) => r.toUpperCase()));
}

export class RegionGuard {
  private readonly allowed: Set<string> | null;
  private readonly blocked: Set<string> | null;
  private readonly lookup: LookupFn;
  private readonly ttlMs: number;
  private readonly onLookupFailure: OnLookupFailure;
  private cachedCountry: string | null = null;
  private cachedAt = 0;

  constructor(opts: RegionGuardOptions = {}) {
    const allowed = opts.allowedRegions ? toUpperSet(opts.allowedRegions) : null;
    const blocked = opts.blockedRegions ? toUpperSet(opts.blockedRegions) : null;
    const hasAllowed = allowed !== null && allowed.size > 0;
    const hasBlocked = blocked !== null && blocked.size > 0;
    if (hasAllowed === hasBlocked) {
      throw new RangeError(
        "specify exactly one of allowedRegions / blockedRegions",
      );
    }
    this.allowed = hasAllowed ? allowed : null;
    this.blocked = hasBlocked ? blocked : null;
    this.lookup = opts.lookup ?? defaultLookup;
    this.ttlMs = opts.ttlMs ?? 300_000;
    this.onLookupFailure = opts.onLookupFailure ?? "block";
  }

  private async resolveCountry(): Promise<string> {
    const now = performance.now();
    if (this.cachedCountry !== null && now - this.cachedAt < this.ttlMs) {
      return this.cachedCountry;
    }
    const country = (await this.lookup()).toUpperCase();
    this.cachedCountry = country;
    this.cachedAt = now;
    return country;
  }

  /** Throw RegionError if the public IP violates the configured mode (whitelist:
   *  not in allowedRegions; blacklist: in blockedRegions), or if the lookup fails
   *  under onLookupFailure='block'. Resolves with no value otherwise. */
  async check(): Promise<void> {
    let country: string;
    try {
      country = await this.resolveCountry();
    } catch (e) {
      if (this.onLookupFailure === "allow") return;
      throw new RegionError(`region lookup failed (${String(e)}); blocking call`);
    }
    if (this.allowed !== null) {
      if (!this.allowed.has(country)) {
        throw new RegionError(
          `public IP in ${country}; expected one of ${[...this.allowed]
            .sort()
            .join(", ")} — is your VPN connected?`,
        );
      }
    } else if (this.blocked!.has(country)) {
      throw new RegionError(
        `public IP in ${country}, a blocked region — is your VPN connected?`,
      );
    }
  }
}
