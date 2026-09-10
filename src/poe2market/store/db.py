"""SQLite storage: writes from the collector, reads from the MCP server.

WAL mode is what makes the two-process split safe — the collector writes while
the MCP server reads, without either blocking the other.

Charts never read :table:`price_sample`. Raw samples are rolled up into hourly
and daily candles and then pruned, which is what keeps a multi-league history
queryable from a single file.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_hour(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def floor_day(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS is a
    # no-op on an existing table, so new columns need an explicit ALTER.
    _ADDED_COLUMNS = {
        "stash_item": [
            ("rarity", "TEXT"),
            ("ilvl", "INTEGER"),
            ("identified", "INTEGER"),
            ("category", "TEXT"),
            ("icon", "TEXT"),
        ],
    }

    def _migrate(self, c: sqlite3.Connection) -> None:
        for table, columns in self._ADDED_COLUMNS.items():
            existing = {
                r["name"] for r in c.execute(f"PRAGMA table_info({table})")
            }
            for name, decl in columns:
                if name not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    log.info("migrated: added %s.%s", table, name)

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA_PATH.read_text())
            self._migrate(c)
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )

    # -- dimensions -----------------------------------------------------

    def league_id(self, name: str, realm: str = "poe2") -> int:
        now = iso(utcnow())
        with self.conn() as c:
            c.execute(
                "INSERT INTO league (name, realm, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name, realm) DO UPDATE SET last_seen=excluded.last_seen",
                (name, realm, now, now),
            )
            row = c.execute(
                "SELECT id FROM league WHERE name=? AND realm=?", (name, realm)
            ).fetchone()
            return int(row["id"])

    def upsert_item(
        self,
        key: str,
        kind: str,
        label: str,
        *,
        name: str | None = None,
        type_: str | None = None,
        currency_id: str | None = None,
        category: str | None = None,
        icon: str | None = None,
        watchlist: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> int:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO item (key, kind, label, name, type, currency_id,
                                  category, icon, watchlist, query_json, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    label=excluded.label,
                    watchlist=COALESCE(excluded.watchlist, item.watchlist),
                    query_json=COALESCE(excluded.query_json, item.query_json)
                """,
                (
                    key, kind, label, name, type_, currency_id, category, icon,
                    watchlist, json.dumps(query) if query else None, iso(utcnow()),
                ),
            )
            return int(
                c.execute("SELECT id FROM item WHERE key=?", (key,)).fetchone()["id"]
            )

    # -- writes ---------------------------------------------------------

    def record_sample(
        self,
        item_id: int,
        league_id: int,
        *,
        ts: datetime,
        source: str,
        n_listings: int,
        currency: str | None,
        low: float | None = None,
        p25: float | None = None,
        median: float | None = None,
        p75: float | None = None,
        high: float | None = None,
        price_base: float | None = None,
        base_currency: str | None = None,
        total_stock: int = 0,
        listings: list[dict[str, Any]] | None = None,
    ) -> int:
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT INTO price_sample
                    (item_id, league_id, ts, source, n_listings, currency,
                     low, p25, median, p75, high, price_base, base_currency,
                     total_stock)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id, league_id, ts, source) DO UPDATE SET
                    n_listings=excluded.n_listings, median=excluded.median,
                    price_base=excluded.price_base, total_stock=excluded.total_stock
                """,
                (
                    item_id, league_id, iso(ts), source, n_listings, currency,
                    low, p25, median, p75, high, price_base, base_currency,
                    total_stock,
                ),
            )
            sample_id = cur.lastrowid or int(
                c.execute(
                    "SELECT id FROM price_sample WHERE item_id=? AND league_id=? "
                    "AND ts=? AND source=?",
                    (item_id, league_id, iso(ts), source),
                ).fetchone()["id"]
            )

            for L in listings or []:
                c.execute(
                    """
                    INSERT INTO listing
                        (sample_id, listing_hash, account, character_name,
                         is_online, price_amount, price_currency, price_base,
                         stock, indexed_at, whisper, item_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sample_id,
                        L.get("listing_hash") or "",
                        L.get("account"),
                        L.get("character_name"),
                        1 if L.get("is_online") else 0,
                        L.get("price_amount"),
                        L.get("price_currency"),
                        L.get("price_base"),
                        L.get("stock"),
                        L.get("indexed_at"),
                        L.get("whisper"),
                        json.dumps(L["item"]) if L.get("item") else None,
                    ),
                )
            return sample_id

    def log_run(
        self,
        job: str,
        league: str | None,
        started: datetime,
        status: str,
        *,
        requests_used: int = 0,
        items_priced: int = 0,
        detail: str | None = None,
    ) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO collector_run (job, league, started_at, finished_at, "
                "status, requests_used, items_priced, detail) VALUES (?,?,?,?,?,?,?,?)",
                (
                    job, league, iso(started), iso(utcnow()), status,
                    requests_used, items_priced, detail,
                ),
            )

    # -- rollups & retention --------------------------------------------

    def rollup(self, since: datetime | None = None) -> dict[str, int]:
        """Rebuild hourly and daily candles from raw samples.

        Recomputes whole buckets rather than incrementing, so a re-run is
        idempotent and a collector restart cannot double-count.
        """
        since = since or (utcnow() - timedelta(days=2))
        counts = {}
        # strftime buckets in SQLite, so rows never leave the database.
        for table, sql_fmt in (("price_hourly", "%Y-%m-%dT%H:00:00Z"),
                               ("price_daily", "%Y-%m-%dT00:00:00Z")):
            with self.conn() as c:
                c.execute(
                    f"""
                    INSERT INTO {table}
                        (item_id, league_id, bucket, open, high, low, close,
                         mean, samples, listings_seen, stock_mean, base_currency)
                    SELECT
                        item_id, league_id,
                        strftime('{sql_fmt}', ts) AS bucket,
                        (SELECT s2.price_base FROM price_sample s2
                          WHERE s2.item_id=s.item_id AND s2.league_id=s.league_id
                            AND strftime('{sql_fmt}', s2.ts)=strftime('{sql_fmt}', s.ts)
                            AND s2.price_base IS NOT NULL
                          ORDER BY s2.ts ASC LIMIT 1),
                        -- Widen the candle to each sample's own book range,
                        -- but only for exchange rows: their low/high are bid
                        -- and ask in the base currency. Search rows carry
                        -- percentiles in whatever the seller listed, so mixing
                        -- them in would compare divine against exalted.
                        MAX(CASE WHEN source='exchange'
                                 THEN COALESCE(high, price_base)
                                 ELSE price_base END),
                        MIN(CASE WHEN source='exchange'
                                 THEN COALESCE(low, price_base)
                                 ELSE price_base END),
                        (SELECT s3.price_base FROM price_sample s3
                          WHERE s3.item_id=s.item_id AND s3.league_id=s.league_id
                            AND strftime('{sql_fmt}', s3.ts)=strftime('{sql_fmt}', s.ts)
                            AND s3.price_base IS NOT NULL
                          ORDER BY s3.ts DESC LIMIT 1),
                        AVG(price_base), COUNT(*), SUM(n_listings),
                        AVG(total_stock), MAX(base_currency)
                    FROM price_sample s
                    WHERE ts >= ? AND price_base IS NOT NULL
                    GROUP BY item_id, league_id, bucket
                    ON CONFLICT(item_id, league_id, bucket) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, mean=excluded.mean,
                        samples=excluded.samples,
                        listings_seen=excluded.listings_seen,
                        stock_mean=excluded.stock_mean
                    """,
                    (iso(since),),
                )
                counts[table] = c.execute(
                    f"SELECT COUNT(*) n FROM {table}"
                ).fetchone()["n"]
        return counts

    def prune(self, retention_days: int) -> dict[str, int]:
        """Drop raw samples past the retention window; rollups keep the history."""
        cutoff = iso(utcnow() - timedelta(days=retention_days))
        with self.conn() as c:
            listings = c.execute(
                "DELETE FROM listing WHERE sample_id IN "
                "(SELECT id FROM price_sample WHERE ts < ?)",
                (cutoff,),
            ).rowcount
            samples = c.execute(
                "DELETE FROM price_sample WHERE ts < ?", (cutoff,)
            ).rowcount
            c.execute("DELETE FROM rate_limit_hits WHERE ts < ?",
                      ((utcnow() - timedelta(days=1)).timestamp(),))
        return {"listings_deleted": listings, "samples_deleted": samples}

    def vacuum(self) -> None:
        c = sqlite3.connect(self.path)
        try:
            c.execute("VACUUM")
        finally:
            c.close()

    # -- reads ----------------------------------------------------------

    def latest(self, item_key: str, league: str) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute(
                """
                SELECT i.key, i.label, i.kind, s.*
                FROM price_sample s
                JOIN item i ON i.id = s.item_id
                JOIN league g ON g.id = s.league_id
                WHERE i.key = ? AND g.name = ?
                ORDER BY s.ts DESC LIMIT 1
                """,
                (item_key, league),
            ).fetchone()
            return dict(row) if row else None

    def history(
        self,
        item_key: str,
        league: str,
        *,
        days: int = 30,
        resolution: str = "auto",
    ) -> list[dict[str, Any]]:
        """Candles for one series. ``auto`` picks hourly under 14 days."""
        if resolution == "auto":
            resolution = "hourly" if days <= 14 else "daily"
        table = "price_hourly" if resolution == "hourly" else "price_daily"
        since = iso(utcnow() - timedelta(days=days))
        with self.conn() as c:
            rows = c.execute(
                f"""
                SELECT r.bucket, r.open, r.high, r.low, r.close, r.mean,
                       r.samples, r.listings_seen, r.stock_mean, r.base_currency
                FROM {table} r
                JOIN item i ON i.id = r.item_id
                JOIN league g ON g.id = r.league_id
                WHERE i.key = ? AND g.name = ? AND r.bucket >= ?
                ORDER BY r.bucket ASC
                """,
                (item_key, league, since),
            ).fetchall()
            return [dict(r) for r in rows]

    def movers(
        self, league: str, *, days: int = 1, limit: int = 20, min_samples: int = 3
    ) -> list[dict[str, Any]]:
        """Largest percentage moves over the window, from daily candles."""
        since = iso(floor_day(utcnow() - timedelta(days=days)))
        with self.conn() as c:
            rows = c.execute(
                """
                WITH win AS (
                    SELECT r.item_id,
                           MIN(r.bucket) AS first_b, MAX(r.bucket) AS last_b,
                           SUM(r.samples) AS n
                    FROM price_daily r
                    JOIN league g ON g.id = r.league_id
                    WHERE g.name = ? AND r.bucket >= ?
                    GROUP BY r.item_id
                    HAVING n >= ?
                )
                SELECT i.key, i.label, i.kind,
                       f.open AS start_price, l.close AS end_price,
                       l.base_currency, win.n AS samples,
                       CASE WHEN f.open > 0
                            THEN (l.close - f.open) / f.open * 100.0 END AS pct_change
                FROM win
                JOIN item i ON i.id = win.item_id
                JOIN price_daily f ON f.item_id = win.item_id AND f.bucket = win.first_b
                JOIN price_daily l ON l.item_id = win.item_id AND l.bucket = win.last_b
                WHERE pct_change IS NOT NULL
                ORDER BY ABS(pct_change) DESC
                LIMIT ?
                """,
                (league, since, min_samples, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_items(
        self, *, league: str | None = None, kind: str | None = None,
        watchlist: str | None = None, query: str | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        if kind:
            where.append("i.kind = ?")
            params.append(kind)
        if watchlist:
            where.append("i.watchlist = ?")
            params.append(watchlist)
        if query:
            where.append("(i.label LIKE ? OR i.key LIKE ? OR i.name LIKE ?)")
            params += [f"%{query}%"] * 3
        join = ""
        if league:
            join = ("JOIN price_sample s ON s.item_id = i.id "
                    "JOIN league g ON g.id = s.league_id AND g.name = ?")
            params.insert(0, league)
        params.append(limit)
        with self.conn() as c:
            rows = c.execute(
                f"SELECT DISTINCT i.key, i.label, i.kind, i.watchlist, i.currency_id "
                f"FROM item i {join} WHERE {' AND '.join(where)} "
                f"ORDER BY i.label LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def currency_rate(
        self,
        currency_id: str,
        league: str,
        at: datetime | None = None,
        side: str = "mid",
    ) -> float | None:
        """Price of one unit of ``currency_id`` in the base currency.

        Takes the nearest sample at or before ``at``, so historical rows convert
        at the rate that actually applied then rather than today's.

        ``side`` matters more than it looks. PoE2 books can show a 90% bid/ask
        spread, and the right side depends on the question:

        * ``ask``  — what you would pay. Use when costing a purchase.
        * ``bid``  — what you would receive. Use when valuing a stash, so the
          number is what you could actually realise rather than a hopeful one.
        * ``mid``  — the midpoint. Fine for trend charts, misleading for money.

        Falls back to whichever side exists when the requested one does not.
        """
        at = at or utcnow()
        column = {"ask": "s.high", "bid": "s.low"}.get(side, "s.price_base")
        with self.conn() as c:
            row = c.execute(
                f"""
                SELECT COALESCE({column}, s.price_base) AS price_base
                FROM price_sample s
                JOIN item i ON i.id = s.item_id
                JOIN league g ON g.id = s.league_id
                WHERE i.currency_id = ? AND g.name = ? AND s.ts <= ?
                  AND COALESCE({column}, s.price_base) IS NOT NULL
                -- Prefer poe.ninja (in-game exchange) over thin trade2 data.
                ORDER BY (s.source = 'ninja') DESC, s.ts DESC LIMIT 1
                """,
                (currency_id, league, iso(at)),
            ).fetchone()
            return float(row["price_base"]) if row else None

    def record_pair_rates(
        self, league_id: int, ts: datetime, rows: list[dict[str, Any]]
    ) -> int:
        with self.conn() as c:
            c.executemany(
                "INSERT INTO pair_rate (league_id, ts, from_currency, "
                "to_currency, rate, best_rate, depth, n_offers, n_raw) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(league_id, ts, from_currency, to_currency) "
                "DO UPDATE SET rate=excluded.rate, best_rate=excluded.best_rate",
                [
                    (league_id, iso(ts), r["from"], r["to"], r["rate"],
                     r.get("best_rate"), r.get("depth", 0),
                     r.get("n_offers", 0), r.get("n_raw", 0))
                    for r in rows
                ],
            )
        return len(rows)

    def latest_pair_rates(
        self, league: str, max_age_minutes: int = 120
    ) -> list[dict[str, Any]]:
        """Most recent rate for each ordered pair, within an age window.

        Stale legs make a cycle unexecutable, so old rows are excluded rather
        than quietly propped up.
        """
        cutoff = iso(utcnow() - timedelta(minutes=max_age_minutes))
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT p.from_currency, p.to_currency, p.rate, p.best_rate,
                       p.depth, p.n_offers, p.ts
                FROM pair_rate p
                JOIN league g ON g.id = p.league_id
                WHERE g.name = ? AND p.ts >= ?
                  AND p.id IN (
                      SELECT MAX(id) FROM pair_rate
                      WHERE league_id = p.league_id
                      GROUP BY from_currency, to_currency
                  )
                """,
                (league, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]

    def liquidity_report(
        self, league: str, *, since_minutes: int = 180
    ) -> list[dict[str, Any]]:
        """Rank currencies by how tradeable they actually are.

        Two-sided liquidity is the thing that matters: a currency quoted only
        on the ask side has no one buying, so it cannot close a trade loop and
        cannot be sold at the quoted price.
        """
        cutoff = iso(utcnow() - timedelta(minutes=since_minutes))
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT i.currency_id, i.label,
                       AVG(p.price_base)  AS price,
                       AVG(p.n_listings)  AS offers,
                       SUM(CASE WHEN p.low IS NOT NULL AND p.high IS NOT NULL
                                THEN 1 ELSE 0 END) AS two_sided_samples,
                       COUNT(*) AS samples,
                       MAX(p.total_stock) AS depth
                FROM price_sample p
                JOIN item i ON i.id = p.item_id
                JOIN league g ON g.id = p.league_id
                WHERE g.name = ? AND p.source = 'exchange' AND p.ts >= ?
                  AND i.currency_id IS NOT NULL
                GROUP BY i.currency_id
                HAVING price IS NOT NULL
                ORDER BY two_sided_samples DESC, offers DESC
                """,
                (league, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_meta(self, key: str) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def latest_stash_snapshot(self, league: str) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute(
                "SELECT s.* FROM stash_snapshot s JOIN league g ON g.id = s.league_id "
                "WHERE g.name = ? ORDER BY s.ts DESC LIMIT 1",
                (league,),
            ).fetchone()
            return dict(row) if row else None

    def stash_items(
        self,
        snapshot_id: int,
        *,
        search: str | None = None,
        tab: str | None = None,
        rarity: str | None = None,
        priced_only: bool = False,
        group_stacks: bool = True,
        sort: str = "value",
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Inventory rows plus totals for the whole filtered set.

        Grouping aggregates a item across tabs, since "how many Exalted Orbs do
        I have" is nearly always the real question rather than "how are they
        distributed across stacks".
        """
        where = ["snapshot_id = ?"]
        params: list[Any] = [snapshot_id]
        if search:
            where.append(
                "(COALESCE(name,'') LIKE ? OR COALESCE(type_line,'') LIKE ?)"
            )
            params += [f"%{search}%", f"%{search}%"]
        if tab:
            where.append("tab_name LIKE ?")
            params.append(f"%{tab}%")
        if rarity:
            where.append("rarity = ? COLLATE NOCASE")
            params.append(rarity)
        if priced_only:
            where.append("value_base IS NOT NULL")
        clause = " AND ".join(where)

        order = {
            "value": "total_value DESC",
            "quantity": "quantity DESC",
            "name": "display ASC",
        }.get(sort, "total_value DESC")

        if group_stacks:
            sql = f"""
                SELECT COALESCE(name, type_line) AS display,
                       type_line, name, rarity,
                       SUM(stack_size) AS quantity,
                       COUNT(*)        AS stacks,
                       MAX(unit_base)  AS unit_value,
                       SUM(value_base) AS total_value,
                       GROUP_CONCAT(DISTINCT tab_name) AS tabs,
                       MIN(priced_from) AS priced_from,
                       MAX(ilvl) AS ilvl
                FROM stash_item WHERE {clause}
                GROUP BY COALESCE(name, type_line), type_line, rarity
                ORDER BY {order} LIMIT ? OFFSET ?
            """
        else:
            sql = f"""
                SELECT COALESCE(name, type_line) AS display,
                       type_line, name, rarity,
                       stack_size AS quantity, 1 AS stacks,
                       unit_base AS unit_value, value_base AS total_value,
                       tab_name AS tabs, priced_from, ilvl
                FROM stash_item WHERE {clause}
                ORDER BY {order} LIMIT ? OFFSET ?
            """
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(sql, params + [limit, offset])]
            t = c.execute(
                f"SELECT COUNT(*) AS stacks, COALESCE(SUM(stack_size),0) AS units, "
                f"COALESCE(SUM(value_base),0) AS value FROM stash_item WHERE {clause}",
                params,
            ).fetchone()
        return rows, dict(t)

    def stats(self) -> dict[str, Any]:
        with self.conn() as c:
            out: dict[str, Any] = {}
            for t in ("item", "price_sample", "listing", "price_hourly",
                      "price_daily", "stash_snapshot", "collector_run"):
                out[t] = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            row = c.execute(
                "SELECT MIN(ts) a, MAX(ts) b FROM price_sample"
            ).fetchone()
            out["earliest_sample"], out["latest_sample"] = row["a"], row["b"]
            out["db_bytes"] = Path(self.path).stat().st_size
            out["leagues"] = [
                dict(r) for r in c.execute(
                    "SELECT name, realm, first_seen, last_seen FROM league"
                ).fetchall()
            ]
            return out
