"""HTTP client for GGG's Path of Exile 2 endpoints.

Everything goes through :meth:`GGGClient.request`, which spends a slot from the
shared :class:`~poe2market.ratelimit.RateLimiter` before each call and feeds the
response's rate-limit headers back in.

Limiter buckets are keyed by *endpoint kind* (``trade-search``, ``trade-fetch``,
``trade-exchange``) rather than by the server's policy name. The server only
reveals its policy name in the response, which is too late to decide whether we
were allowed to send it; the endpoint kind maps onto the same buckets and is
known up front.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from ..ratelimit import RateLimiter

log = logging.getLogger(__name__)

TRADE_BASE = "https://www.pathofexile.com/api/trade2"
API_BASE = "https://api.pathofexile.com"

MAX_RETRIES = 4


class GGGError(RuntimeError):
    """An API call failed in a way the caller should see."""


class RateLimited(GGGError):
    """Hit a 429 more times than we are willing to retry."""


class GGGClient:
    def __init__(
        self,
        user_agent: str,
        limiter: RateLimiter,
        timeout: float = 30.0,
    ) -> None:
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._client = httpx.AsyncClient(
            headers=headers, timeout=timeout, follow_redirects=True
        )
        self._limiter = limiter
        self.policy_names: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GGGClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- core ----------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        bucket: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            await self._limiter.acquire(bucket)
            try:
                resp = await self._client.request(
                    method, url, json=json_body, params=params
                )
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(min(2**attempt, 20))
                continue

            self._learn(bucket, resp)

            if resp.status_code == 429:
                retry_after = _float_header(resp, "Retry-After")
                wait = self._limiter.note_429(bucket, retry_after)
                last_error = RateLimited(f"429 on {url} (waited {wait:.0f}s)")
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (500, 502, 503, 504):
                last_error = GGGError(f"{resp.status_code} on {url}")
                await asyncio.sleep(min(2**attempt, 20))
                continue

            if resp.status_code >= 400:
                raise GGGError(
                    f"{resp.status_code} on {url}: {resp.text[:300]}"
                )

            try:
                return resp.json()
            except ValueError as exc:
                raise GGGError(f"Non-JSON response from {url}") from exc

        raise last_error or GGGError(f"Request to {url} failed")

    def _learn(self, bucket: str, resp: httpx.Response) -> None:
        """Feed this response's rate-limit headers back into the limiter."""
        policy = resp.headers.get("X-Rate-Limit-Policy")
        if policy:
            self.policy_names[bucket] = policy

        # X-Rate-Limit-Rules names which per-scope headers are present, e.g.
        # "Ip" -> X-Rate-Limit-Ip and X-Rate-Limit-Ip-State.
        rules_header = resp.headers.get("X-Rate-Limit-Rules", "")
        for scope in [s.strip() for s in rules_header.split(",") if s.strip()]:
            self._limiter.observe(
                bucket,
                resp.headers.get(f"X-Rate-Limit-{scope}"),
                scope,
                resp.headers.get(f"X-Rate-Limit-{scope}-State"),
            )

    def budget(self) -> dict[str, Any]:
        """Remaining request budget per bucket, for the status tool."""
        return {
            bucket: {
                "policy": self.policy_names.get(bucket),
                "rules": self._limiter.budget(bucket),
            }
            for bucket in ("trade-search", "trade-fetch", "trade-exchange", "trade-data")
        }


def _float_header(resp: httpx.Response, name: str) -> float | None:
    raw = resp.headers.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def league_path(league: str) -> str:
    return quote(league, safe="")
