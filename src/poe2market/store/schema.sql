-- POE2MarketMCP schema.
--
-- Three tiers, because 46M raw rows/year is too many to chart from directly:
--   price_sample  raw observations, pruned after raw_retention_days
--   price_hourly  OHLC rollup, what charts under ~30 days read
--   price_daily   OHLC rollup, what league-long charts read
--
-- Prices are stored both as listed (amount + currency) and normalised to the
-- configured base currency, so a chart can span periods where the item was
-- listed in different currencies.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS league (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    realm      TEXT NOT NULL DEFAULT 'poe2',
    is_active  INTEGER NOT NULL DEFAULT 1,
    -- Temp leagues restart; keeping first_seen lets us separate two leagues
    -- that reuse a name (e.g. successive "Standard" snapshots stay one row).
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE (name, realm)
);

-- The series dimension: one row per priceable thing.
CREATE TABLE IF NOT EXISTS item (
    id           INTEGER PRIMARY KEY,
    key          TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,           -- currency | unique | base | raw
    label        TEXT NOT NULL,
    name         TEXT,
    type         TEXT,
    currency_id  TEXT,                    -- exchange id, for kind=currency
    category     TEXT,
    icon         TEXT,
    watchlist    TEXT,                    -- which list introduced it
    query_json   TEXT,                    -- trade2 query used, for reproducibility
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_kind ON item (kind);
CREATE INDEX IF NOT EXISTS idx_item_watchlist ON item (watchlist);

-- One row per scan of one item in one league.
CREATE TABLE IF NOT EXISTS price_sample (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES item (id) ON DELETE CASCADE,
    league_id     INTEGER NOT NULL REFERENCES league (id) ON DELETE CASCADE,
    ts            TEXT NOT NULL,          -- ISO-8601 UTC
    source        TEXT NOT NULL,          -- exchange | search
    n_listings    INTEGER NOT NULL DEFAULT 0,
    currency      TEXT,                   -- currency the raw stats are in
    low           REAL,
    p25           REAL,
    median        REAL,
    p75           REAL,
    high          REAL,
    -- median converted to the base currency at ts; the charting value.
    price_base    REAL,
    base_currency TEXT,
    total_stock   INTEGER,                -- exchange depth, when known
    UNIQUE (item_id, league_id, ts, source)
);
-- Covering index for the chart query: one item, one league, a time range.
CREATE INDEX IF NOT EXISTS idx_sample_series
    ON price_sample (item_id, league_id, ts);
CREATE INDEX IF NOT EXISTS idx_sample_ts ON price_sample (ts);

-- Individual listings, kept briefly. Powers trade preparation (whisper
-- strings) and order-book depth; pruned aggressively since it is the bulkiest
-- table by far.
CREATE TABLE IF NOT EXISTS listing (
    id             INTEGER PRIMARY KEY,
    sample_id      INTEGER NOT NULL REFERENCES price_sample (id) ON DELETE CASCADE,
    listing_hash   TEXT NOT NULL,
    account        TEXT,
    character_name TEXT,
    is_online      INTEGER NOT NULL DEFAULT 0,
    price_amount   REAL,
    price_currency TEXT,
    price_base     REAL,
    stock          INTEGER,
    indexed_at     TEXT,
    whisper        TEXT,
    item_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_listing_sample ON listing (sample_id);
CREATE INDEX IF NOT EXISTS idx_listing_hash ON listing (listing_hash);

-- Rollups. bucket is the ISO timestamp of the bucket start.
CREATE TABLE IF NOT EXISTS price_hourly (
    item_id       INTEGER NOT NULL REFERENCES item (id) ON DELETE CASCADE,
    league_id     INTEGER NOT NULL REFERENCES league (id) ON DELETE CASCADE,
    bucket        TEXT NOT NULL,
    open          REAL, high REAL, low REAL, close REAL, mean REAL,
    samples       INTEGER NOT NULL DEFAULT 0,
    listings_seen INTEGER NOT NULL DEFAULT 0,
    stock_mean    REAL,
    base_currency TEXT,
    PRIMARY KEY (item_id, league_id, bucket)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS price_daily (
    item_id       INTEGER NOT NULL REFERENCES item (id) ON DELETE CASCADE,
    league_id     INTEGER NOT NULL REFERENCES league (id) ON DELETE CASCADE,
    bucket        TEXT NOT NULL,
    open          REAL, high REAL, low REAL, close REAL, mean REAL,
    samples       INTEGER NOT NULL DEFAULT 0,
    listings_seen INTEGER NOT NULL DEFAULT 0,
    stock_mean    REAL,
    base_currency TEXT,
    PRIMARY KEY (item_id, league_id, bucket)
) WITHOUT ROWID;

-- Stash snapshots, for portfolio valuation and P&L over time.
CREATE TABLE IF NOT EXISTS stash_snapshot (
    id           INTEGER PRIMARY KEY,
    league_id    INTEGER NOT NULL REFERENCES league (id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    account      TEXT,
    tab_count    INTEGER NOT NULL DEFAULT 0,
    item_count   INTEGER NOT NULL DEFAULT 0,
    total_base   REAL,                    -- valuation in base currency
    base_currency TEXT
);
CREATE INDEX IF NOT EXISTS idx_stash_ts ON stash_snapshot (league_id, ts);

CREATE TABLE IF NOT EXISTS stash_item (
    id           INTEGER PRIMARY KEY,
    snapshot_id  INTEGER NOT NULL REFERENCES stash_snapshot (id) ON DELETE CASCADE,
    item_id      INTEGER REFERENCES item (id) ON DELETE SET NULL,
    tab_name     TEXT,
    tab_index    INTEGER,
    name         TEXT,
    type_line    TEXT,
    stack_size   INTEGER NOT NULL DEFAULT 1,
    unit_base    REAL,                    -- unit price in base currency
    value_base   REAL,                    -- unit_base * stack_size
    priced_from  TEXT,                    -- exchange | search | unpriced
    rarity       TEXT,                    -- Normal | Magic | Rare | Unique | Currency
    ilvl         INTEGER,
    identified   INTEGER,
    category     TEXT,                    -- coarse bucket for filtering
    icon         TEXT,
    raw_json     TEXT                     -- full item payload, for mods etc.
);
CREATE INDEX IF NOT EXISTS idx_stash_item_type ON stash_item (type_line);
CREATE INDEX IF NOT EXISTS idx_stash_item_snapshot ON stash_item (snapshot_id);

-- Collector observability: what ran, what it cost, what broke.
CREATE TABLE IF NOT EXISTS collector_run (
    id             INTEGER PRIMARY KEY,
    job            TEXT NOT NULL,
    league         TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,         -- ok | partial | error
    requests_used  INTEGER NOT NULL DEFAULT 0,
    items_priced   INTEGER NOT NULL DEFAULT 0,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_started ON collector_run (started_at);

-- Shared rate-limit ledger. Owned here so the schema is complete on its own;
-- RateLimiter also creates it defensively for standalone use.
CREATE TABLE IF NOT EXISTS rate_limit_hits (
    policy TEXT NOT NULL,
    ts     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rl_policy_ts ON rate_limit_hits (policy, ts);

-- Learned rate-limit rules, so a freshly started process knows the budget
-- before it has made its first request.
CREATE TABLE IF NOT EXISTS rate_limit_rules (
    policy     TEXT PRIMARY KEY,
    rules      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Direct currency-to-currency rates.
--
-- Everything else is priced against one base currency, which makes every
-- cross-rate synthetic: a cycle built from base-relative quotes can never show
-- a profit, because each leg pays the spread. Real multi-hop arbitrage lives in
-- *direct* pair quotes, where the market's own chaos->divine rate can drift
-- away from chaos->exalted->divine. One request prices 10 pairs, so a set of N
-- currencies costs N requests to sweep completely.
CREATE TABLE IF NOT EXISTS pair_rate (
    id          INTEGER PRIMARY KEY,
    league_id   INTEGER NOT NULL REFERENCES league (id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    from_currency TEXT NOT NULL,   -- what you give
    to_currency   TEXT NOT NULL,   -- what you receive
    rate        REAL NOT NULL,     -- units of `to` received per 1 `from`
    best_rate   REAL,              -- most favourable in the cluster
    depth       INTEGER NOT NULL DEFAULT 0,
    n_offers    INTEGER NOT NULL DEFAULT 0,
    n_raw       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (league_id, ts, from_currency, to_currency)
);
CREATE INDEX IF NOT EXISTS idx_pair_lookup
    ON pair_rate (league_id, from_currency, to_currency, ts);
CREATE INDEX IF NOT EXISTS idx_pair_ts ON pair_rate (ts);
