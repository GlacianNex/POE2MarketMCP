"""GGG rate-limit policy engine.

Every Path of Exile API response carries its own limits, e.g.::

    X-Rate-Limit-Rules: Ip
    X-Rate-Limit-Policy: trade-search-request-limit
    X-Rate-Limit-Ip: 5:10:60,15:60:300,30:300:1800,600:21600:3600
    X-Rate-Limit-Ip-State: 1:10:0,1:60:0,1:300:0,1:21600:0

Each triple is ``max_hits:period_seconds:restrict_seconds``. The server is the
source of truth, so rules are learned from responses rather than hardcoded.

Limits are enforced per *IP*, but the collector daemon and the MCP server are
separate processes sharing that IP. An in-process limiter would let them each
spend the full budget and collect a restriction. The ledger of spent requests
therefore lives in SQLite so both processes draw down one shared budget.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Stop this far short of the stated maximum. A restriction costs minutes of
# downtime; a slightly slower sweep costs nothing.
DEFAULT_HEADROOM = 1


@dataclass(frozen=True)
class Rule:
    """One ``max_hits:period:restrict`` triple from a rate-limit header."""

    max_hits: int
    period: float
    restrict: float

    @property
    def key(self) -> str:
        return f"{self.max_hits}:{int(self.period)}:{int(self.restrict)}"


def parse_rules(header: str | None) -> list[Rule]:
    """Parse ``5:10:60,15:60:300`` into Rules, skipping malformed entries."""
    if not header:
        return []
    rules: list[Rule] = []
    for chunk in header.split(","):
        parts = chunk.strip().split(":")
        if len(parts) != 3:
            continue
        try:
            rules.append(Rule(int(parts[0]), float(parts[1]), float(parts[2])))
        except ValueError:
            continue
    return rules


class RateLimiter:
    """Sliding-window limiter backed by a shared SQLite ledger.

    One instance covers every policy; rules are keyed by policy name so
    ``trade-search-request-limit`` and ``trade-exchange-request-limit`` draw
    down independent budgets, exactly as the server tracks them.
    """

    def __init__(self, db_path: str, headroom: int = DEFAULT_HEADROOM) -> None:
        self._db_path = db_path
        self._headroom = headroom
        self._rules: dict[str, list[Rule]] = {}
        # Wall-clock time until which a policy is known to be restricted.
        self._blocked_until: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limit_hits (
                    policy TEXT NOT NULL,
                    ts     REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rl_policy_ts "
                "ON rate_limit_hits (policy, ts)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limit_rules (
                    policy     TEXT PRIMARY KEY,
                    rules      TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        self._load_rules()

    def _load_rules(self) -> None:
        """Restore rules learned by earlier runs.

        Without this a freshly started process reports an unknown budget and
        has to spend a probe request to rediscover limits it already knew.
        """
        with self._connect() as conn:
            for policy, text in conn.execute(
                "SELECT policy, rules FROM rate_limit_rules"
            ):
                rules = parse_rules(text)
                if rules:
                    self._rules[policy] = rules

    def _save_rules(self, policy: str, rules: list[Rule]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rate_limit_rules (policy, rules, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(policy) DO UPDATE SET "
                "rules=excluded.rules, updated_at=excluded.updated_at",
                (policy, ",".join(r.key for r in rules), time.time()),
            )

    # -- rule learning -------------------------------------------------

    def observe(self, policy: str, rules_header: str | None,
                rule_names: str | None, state_header: str | None) -> None:
        """Update known rules from a response's rate-limit headers.

        ``rule_names`` is the ``X-Rate-Limit-Rules`` value (e.g. ``Ip``); the
        caller resolves it to the matching ``X-Rate-Limit-<name>`` header and
        passes the value as ``rules_header``.
        """
        rules = parse_rules(rules_header)
        if rules and rules != self._rules.get(policy):
            self._rules[policy] = rules
            self._save_rules(policy, rules)

        # The -State header reports the server's own count. If it says we are
        # further along than our ledger believes, trust it and top the ledger
        # up so the next window reflects reality.
        for state in parse_rules(state_header):
            if state.restrict > 0:
                self._blocked_until[policy] = time.time() + state.restrict
                log.warning(
                    "policy %s restricted by server for %.0fs", policy, state.restrict
                )

    def note_429(self, policy: str, retry_after: float | None) -> float:
        """Record a 429 and return how long to wait before retrying."""
        wait = retry_after if retry_after else self._max_restrict(policy)
        self._blocked_until[policy] = time.time() + wait
        log.warning("policy %s got 429, backing off %.0fs", policy, wait)
        return wait

    def _max_restrict(self, policy: str) -> float:
        rules = self._rules.get(policy) or []
        return max((r.restrict for r in rules), default=60.0)

    # -- acquisition ---------------------------------------------------

    async def acquire(self, policy: str) -> None:
        """Block until a request against ``policy`` is within every rule."""
        while True:
            async with self._lock:
                wait = self._try_reserve(policy)
            if wait <= 0:
                return
            # Jitter keeps two processes from waking into the same slot.
            await asyncio.sleep(wait + random.uniform(0.05, 0.35))

    def _try_reserve(self, policy: str) -> float:
        """Reserve a slot, or return seconds to wait. Must hold ``_lock``."""
        now = time.time()

        blocked = self._blocked_until.get(policy, 0.0)
        if blocked > now:
            return blocked - now

        rules = self._rules.get(policy)
        if not rules:
            # No rules learned yet: let a single probe through so the first
            # response can teach us the policy.
            self._record(policy, now)
            return 0.0

        longest = max(r.period for r in rules)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM rate_limit_hits WHERE policy = ? AND ts < ?",
                (policy, now - longest),
            )
            waits: list[float] = []
            for rule in rules:
                cutoff = now - rule.period
                row = conn.execute(
                    "SELECT COUNT(*), MIN(ts) FROM rate_limit_hits "
                    "WHERE policy = ? AND ts >= ?",
                    (policy, cutoff),
                ).fetchone()
                hits, oldest = row[0], row[1]
                budget = max(1, rule.max_hits - self._headroom)
                if hits >= budget:
                    # Wait until the oldest hit in this window ages out.
                    waits.append((oldest + rule.period) - now)

            if waits:
                conn.execute("ROLLBACK")
                return max(max(waits), 0.01)

            conn.execute(
                "INSERT INTO rate_limit_hits (policy, ts) VALUES (?, ?)",
                (policy, now),
            )
            conn.execute("COMMIT")
            return 0.0
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _record(self, policy: str, ts: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rate_limit_hits (policy, ts) VALUES (?, ?)", (policy, ts)
            )

    # -- introspection -------------------------------------------------

    def budget(self, policy: str) -> list[dict[str, float]]:
        """Report remaining headroom per rule, for the ``market_status`` tool."""
        rules = self._rules.get(policy) or []
        if not rules:
            return []
        now = time.time()
        out = []
        with self._connect() as conn:
            for rule in rules:
                hits = conn.execute(
                    "SELECT COUNT(*) FROM rate_limit_hits WHERE policy = ? AND ts >= ?",
                    (policy, now - rule.period),
                ).fetchone()[0]
                out.append(
                    {
                        "window_seconds": rule.period,
                        "max_hits": rule.max_hits,
                        "used": hits,
                        "remaining": max(0, rule.max_hits - self._headroom - hits),
                    }
                )
        return out
