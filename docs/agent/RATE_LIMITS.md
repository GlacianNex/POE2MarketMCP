# poe2market — rate limits and API etiquette

Limits are advertised per response and enforced **per IP**. They are learned
from `X-Rate-Limit` headers rather than hardcoded, and persisted so a restarted
process knows the budget before its first request.

## Measured limits (unauthenticated)

| Endpoint | Limit | Batch |
|---|---|---|
| `exchange` | 30 / 300s | up to **10** ids per call, both arrays |
| `search` | 600 / 21600s | 1 query per request |
| `fetch` | 1000 / 21600s | 10 listings per request |

That 10-id cap drives the whole collector design: pricing all ~800 exchangeable
currencies costs ~160 requests per pass, so tiers exist — core orbs every 10
minutes, the long tail every 6 hours.

## Shared budget

The collector daemon and the MCP server are separate processes on one IP. The
ledger of spent requests lives in SQLite so they draw down **one** budget. An
in-process limiter would let each spend the full allowance and earn a
restriction.

Consequence for tools: **every live call you make takes budget from the
collector.** Starving it leaves permanent holes in the history, because GGG
serves only current listings and gaps cannot be backfilled.

## poe.ninja (currency source)

Currency prices come from poe.ninja, not GGG. Its timing, from live headers:

- **CDN-cached 30 min** (`max-age=1800`), served stale 5 min more while
  revalidating.
- Underlying data refreshes **~hourly**.

The collector polls hourly (`ninja_cadence_minutes`) — polling faster
returns the identical cached body. Requests carry a browser-like User-Agent and
`Referer: https://poe.ninja/poe2/economy` (some non-browser requests 404). This
is a public, unversioned, undocumented endpoint — treat it as best effort.

### What actually limits freshness

Measured: a CDN-cached response and a cache-bypassing origin request returned
**byte-identical data** (52 currencies, zero differing). So the 30-minute CDN
cache is not the binding constraint — poe.ninja's own recompute cadence
(~hourly) is. The collector's hourly sweep is matched to that.

`refresh_prices` bypasses the edge cache (verified: `cf-cache-status: MISS` vs
`REVALIDATED`). That guarantees you are not reading a stale edge copy, but it
cannot return numbers newer than poe.ninja has published.

## Etiquette this server follows

- Descriptive `User-Agent` with a contact address, per GGG policy.
- Obeys `X-Rate-Limit` headers, stopping one request short of every limit.
- Honours `Retry-After` on 429; exponential backoff on 5xx.
- Caches aggressively; charts never re-hit the API.
- No in-game automation of any kind.

## Compliance note

`api/trade2` is not in GGG's documented API Reference — it is the endpoint
powering the official trade site. GGG staff have publicly posted its rate
limits in response to third-party tool questions, and the wider tool ecosystem
runs on it, but it carries no stability or support guarantee. The sanctioned
path for bulk market data is the public stash river (`service:psapi`), which
requires an OAuth client granted by GGG.

For personal-scale use with strict rate-limit compliance this is ordinary
third-party behaviour. If usage grows beyond that, apply for a psapi client
rather than scaling this up.
