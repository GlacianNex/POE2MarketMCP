"""Tests for the logic that is easy to get quietly wrong."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from poe2market.config import WatchTarget
from poe2market.ggg.trade import MarketBook, Offer, PricePoint, build_query, _quantile
from poe2market.ratelimit import RateLimiter, Rule, parse_rules
from poe2market.store.db import Store, utcnow


def _db() -> str:
    return str(Path(tempfile.mkdtemp()) / "t.db")


# -- rate limit header parsing -----------------------------------------


def test_parse_rules_reads_ggg_header():
    rules = parse_rules("5:10:60,15:60:300,30:300:1800,600:21600:3600")
    assert rules[0] == Rule(5, 10.0, 60.0)
    assert rules[-1] == Rule(600, 21600.0, 3600.0)


def test_parse_rules_skips_malformed_without_raising():
    assert parse_rules("5:10:60,garbage,,7:x:9") == [Rule(5, 10.0, 60.0)]
    assert parse_rules(None) == []


def test_limiter_blocks_once_budget_is_spent():
    lim = RateLimiter(_db(), headroom=0)
    lim._rules["p"] = [Rule(3, 60.0, 60.0)]
    for _ in range(3):
        assert lim._try_reserve("p") == 0.0
    # Fourth must wait for the oldest hit to age out of the 60s window.
    wait = lim._try_reserve("p")
    assert 0 < wait <= 60.0


def test_limiter_headroom_stops_short_of_the_stated_max():
    lim = RateLimiter(_db(), headroom=1)
    lim._rules["p"] = [Rule(3, 60.0, 60.0)]
    assert lim._try_reserve("p") == 0.0
    assert lim._try_reserve("p") == 0.0
    # Budget is 3, headroom 1, so the third request waits.
    assert lim._try_reserve("p") > 0


def test_limiter_rules_survive_a_new_process():
    path = _db()
    a = RateLimiter(path)
    a.observe("trade-search", "5:10:60,15:60:300", "Ip", None)
    # A separate instance stands in for the MCP server starting fresh.
    b = RateLimiter(path)
    assert b._rules["trade-search"] == parse_rules("5:10:60,15:60:300")


def test_limiter_budget_is_shared_across_instances():
    path = _db()
    a = RateLimiter(path, headroom=0)
    a._rules["p"] = [Rule(2, 60.0, 60.0)]
    b = RateLimiter(path, headroom=0)
    b._rules["p"] = [Rule(2, 60.0, 60.0)]
    assert a._try_reserve("p") == 0.0
    assert b._try_reserve("p") == 0.0
    # Two spent between them means the third waits, whichever asks.
    assert a._try_reserve("p") > 0


# -- market book -------------------------------------------------------


def _offer(price: float, stock: int = 1) -> Offer:
    return Offer("h", "divine", "exalted", price, stock)


def test_book_picks_cheapest_ask_and_richest_bid():
    b = MarketBook.build("divine", "exalted",
                         [_offer(549), _offer(600)], [_offer(540), _offer(500)])
    assert b.best_ask == 549
    assert b.best_bid == 540
    assert b.mid == 544.5


def test_book_spread_flags_an_illiquid_market():
    b = MarketBook.build("divine", "exalted", [_offer(549)], [_offer(202.5)])
    assert b.spread_pct > 90


def test_book_falls_back_to_the_only_side_present():
    ask_only = MarketBook.build("chaos", "exalted", [_offer(13.75)], [])
    assert ask_only.mid == 13.75
    assert ask_only.spread_pct is None

    bid_only = MarketBook.build("chaos", "exalted", [], [_offer(13.75)])
    assert bid_only.mid == 13.75


def test_book_with_no_offers_has_no_price():
    assert MarketBook.build("x", "exalted", [], []).mid is None


# -- price aggregation -------------------------------------------------


def test_quantile_interpolates():
    assert _quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert _quantile([5.0], 0.25) == 5.0


def test_price_point_percentiles():
    p = PricePoint.from_prices([1, 2, 3, 4, 100], "exalted")
    assert p.low == 1 and p.high == 100
    assert p.median == 3
    assert p.n_listings == 5


def test_price_point_handles_no_listings():
    p = PricePoint.from_prices([], "exalted")
    assert p.n_listings == 0 and p.median is None


# -- query building ----------------------------------------------------


def test_build_query_for_a_named_unique():
    q = build_query(WatchTarget(key="k", kind="unique", name="Mageblood",
                                type="Utility Belt"))
    assert q["query"]["name"] == "Mageblood"
    assert q["query"]["type"] == "Utility Belt"
    assert q["sort"] == {"price": "asc"}


def test_build_query_passes_filters_through():
    q = build_query(WatchTarget(
        key="k", kind="base", type="Stellar Amulet",
        filters={"misc_filters": {"filters": {"ilvl": {"min": 82}}}},
    ))
    assert q["query"]["filters"]["misc_filters"]["filters"]["ilvl"]["min"] == 82


def test_raw_query_is_used_verbatim_but_still_sorted():
    q = build_query(WatchTarget(
        key="k", kind="raw",
        raw_query={"query": {"type": "Stellar Amulet"}},
    ))
    assert q["query"] == {"type": "Stellar Amulet"}
    assert q["sort"] == {"price": "asc"}


# -- store -------------------------------------------------------------


@pytest.fixture()
def store() -> Store:
    return Store(_db())


def test_rollup_builds_correct_ohlc(store: Store):
    lg = store.league_id("L")
    it = store.upsert_item("cur:divine", "currency", "Divine Orb",
                           currency_id="divine")
    base = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    for offset, price in ((0, 500.0), (20, 520.0), (40, 480.0)):
        store.record_sample(it, lg, ts=base + timedelta(minutes=offset),
                            source="exchange", n_listings=1, currency="exalted",
                            median=price, price_base=price,
                            base_currency="exalted")
    store.rollup(since=base - timedelta(hours=1))
    c = store.history("cur:divine", "L", days=1, resolution="hourly")[0]
    assert (c["open"], c["high"], c["low"], c["close"]) == (500, 520, 480, 480)


def test_rollup_is_idempotent(store: Store):
    lg = store.league_id("L")
    it = store.upsert_item("k", "currency", "K", currency_id="k")
    ts = utcnow() - timedelta(minutes=5)
    store.record_sample(it, lg, ts=ts, source="exchange", n_listings=1,
                        currency="exalted", median=10.0, price_base=10.0,
                        base_currency="exalted")
    store.rollup(since=ts - timedelta(hours=1))
    first = store.history("k", "L", days=1, resolution="hourly")
    store.rollup(since=ts - timedelta(hours=1))
    assert store.history("k", "L", days=1, resolution="hourly") == first


def test_candle_widens_to_the_book_range_for_exchange_rows(store: Store):
    lg = store.league_id("L")
    it = store.upsert_item("k", "currency", "K", currency_id="k")
    ts = utcnow() - timedelta(minutes=5)
    store.record_sample(it, lg, ts=ts, source="exchange", n_listings=2,
                        currency="exalted", low=90.0, high=110.0,
                        median=100.0, price_base=100.0, base_currency="exalted")
    store.rollup(since=ts - timedelta(hours=1))
    c = store.history("k", "L", days=1, resolution="hourly")[0]
    assert (c["low"], c["high"]) == (90.0, 110.0)


def test_search_rows_do_not_pollute_the_candle_with_foreign_currency(store: Store):
    """A search row's low/high are in the listed currency, not the base."""
    lg = store.league_id("L")
    it = store.upsert_item("u", "unique", "U")
    ts = utcnow() - timedelta(minutes=5)
    # Listed at 220-300 divine; base value 97695 exalted.
    store.record_sample(it, lg, ts=ts, source="search", n_listings=2,
                        currency="divine", low=220.0, high=300.0,
                        median=260.0, price_base=97695.0,
                        base_currency="exalted")
    store.rollup(since=ts - timedelta(hours=1))
    c = store.history("u", "L", days=1, resolution="hourly")[0]
    # The candle must stay in exalted, not collapse to the divine numbers.
    assert c["low"] == 97695.0 and c["high"] == 97695.0


def test_prune_drops_raw_samples_but_keeps_history(store: Store):
    lg = store.league_id("L")
    it = store.upsert_item("k", "currency", "K", currency_id="k")
    ts = utcnow() - timedelta(days=30)
    store.record_sample(it, lg, ts=ts, source="exchange", n_listings=1,
                        currency="exalted", median=5.0, price_base=5.0,
                        base_currency="exalted")
    store.rollup(since=ts - timedelta(days=1))
    assert store.prune(14)["samples_deleted"] == 1
    assert len(store.history("k", "L", days=60, resolution="daily")) == 1


def test_currency_rate_side_selects_bid_or_ask(store: Store):
    lg = store.league_id("L")
    it = store.upsert_item("cur:divine", "currency", "Divine Orb",
                           currency_id="divine")
    store.record_sample(it, lg, ts=utcnow(), source="exchange", n_listings=2,
                        currency="exalted", low=202.5, high=549.0,
                        median=375.75, price_base=375.75,
                        base_currency="exalted")
    assert store.currency_rate("divine", "L", side="ask") == 549.0
    assert store.currency_rate("divine", "L", side="bid") == 202.5
    assert store.currency_rate("divine", "L", side="mid") == 375.75


def test_currency_rate_is_none_when_uncollected(store: Store):
    store.league_id("L")
    assert store.currency_rate("mirror", "L") is None


# -- stash inventory ---------------------------------------------------


def _stash_fixture(store: Store) -> int:
    """A stash with the same item split across tabs, plus unpriced gear."""
    lg = store.league_id("L")
    with store.conn() as c:
        cur = c.execute(
            "INSERT INTO stash_snapshot (league_id, ts, account, tab_count, "
            "item_count, total_base, base_currency) VALUES (?,?,?,?,?,?,?)",
            (lg, "2026-09-09T00:00:00Z", "acct", 2, 4, 0.0, "exalted"),
        )
        snap = cur.lastrowid
        rows = [
            # Same currency, three stacks, two tabs.
            (snap, "Currency", 0, None, "Exalted Orb", 150, 1.0, 150.0,
             "base", "Currency", None),
            (snap, "Currency", 0, None, "Exalted Orb", 200, 1.0, 200.0,
             "base", "Currency", None),
            (snap, "Dump", 1, None, "Exalted Orb", 50, 1.0, 50.0,
             "base", "Currency", None),
            # Unpriced gear must still appear in the inventory.
            (snap, "Dump", 1, "Mageblood", "Utility Belt", 1, None, None,
             "unpriced", "Unique", 84),
        ]
        c.executemany(
            "INSERT INTO stash_item (snapshot_id, tab_name, tab_index, name, "
            "type_line, stack_size, unit_base, value_base, priced_from, "
            "rarity, ilvl) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return snap


def test_stash_groups_stacks_across_tabs(store: Store):
    snap = _stash_fixture(store)
    rows, totals = store.stash_items(snap, group_stacks=True)
    orbs = next(r for r in rows if r["display"] == "Exalted Orb")
    assert orbs["quantity"] == 400      # 150 + 200 + 50
    assert orbs["stacks"] == 3
    assert set(orbs["tabs"].split(",")) == {"Currency", "Dump"}
    assert totals["units"] == 401       # includes the belt


def test_stash_ungrouped_shows_each_stack(store: Store):
    snap = _stash_fixture(store)
    rows, _ = store.stash_items(snap, group_stacks=False)
    orbs = [r for r in rows if r["display"] == "Exalted Orb"]
    assert len(orbs) == 3
    assert sorted(r["quantity"] for r in orbs) == [50, 150, 200]


def test_stash_includes_unpriced_items(store: Store):
    snap = _stash_fixture(store)
    rows, _ = store.stash_items(snap)
    belt = next(r for r in rows if r["display"] == "Mageblood")
    assert belt["quantity"] == 1
    assert belt["total_value"] is None
    assert belt["priced_from"] == "unpriced"


def test_stash_priced_only_excludes_gear(store: Store):
    snap = _stash_fixture(store)
    rows, _ = store.stash_items(snap, priced_only=True)
    assert all(r["display"] != "Mageblood" for r in rows)


def test_stash_search_and_tab_filters(store: Store):
    snap = _stash_fixture(store)
    assert len(store.stash_items(snap, search="Mage")[0]) == 1
    rows, _ = store.stash_items(snap, tab="Currency", group_stacks=False)
    assert len(rows) == 2       # only the two stacks in that tab


def test_stash_rarity_filter(store: Store):
    snap = _stash_fixture(store)
    rows, _ = store.stash_items(snap, rarity="unique")
    assert len(rows) == 1 and rows[0]["display"] == "Mageblood"


# -- league resolution -------------------------------------------------

from poe2market.ggg.leagues import expand_tokens, is_variant, pick_current

LIVE_LEAGUES = [
    {"id": "Forbidden Rites"}, {"id": "HC Forbidden Rites"},
    {"id": "Runes of Aldur"}, {"id": "HC Runes of Aldur"},
    {"id": "Standard"}, {"id": "Hardcore"},
]


def test_current_league_is_the_first_non_permanent_non_variant():
    assert pick_current(LIVE_LEAGUES) == "Forbidden Rites"


def test_current_hardcore_picks_the_matching_counterpart():
    assert pick_current(LIVE_LEAGUES, hardcore=True) == "HC Forbidden Rites"


def test_older_league_is_not_mistaken_for_current():
    """Two challenge leagues can be live at once; order decides."""
    assert pick_current(LIVE_LEAGUES) != "Runes of Aldur"


def test_permanent_only_resolves_to_nothing():
    assert pick_current([{"id": "Standard"}, {"id": "Hardcore"}]) is None


def test_variant_detection():
    assert is_variant("HC Forbidden Rites")
    assert is_variant("SSF Standard")
    assert is_variant("HC SSF Ruthless")
    assert not is_variant("Forbidden Rites")


def test_empty_config_means_current_league():
    assert expand_tokens([], LIVE_LEAGUES) == ["Forbidden Rites"]


def test_explicit_league_names_pass_through_untouched():
    assert expand_tokens(["Standard"], LIVE_LEAGUES) == ["Standard"]


def test_tokens_and_names_can_be_mixed_without_duplicates():
    got = expand_tokens(
        ["@current", "Standard", "@current", "@current-hc"], LIVE_LEAGUES
    )
    assert got == ["Forbidden Rites", "Standard", "HC Forbidden Rites"]


def test_unresolvable_token_is_dropped_not_crashed():
    assert expand_tokens(["@current"], [{"id": "Standard"}]) == []


def test_fast_lane_currencies_are_not_reswept_by_a_later_tier():
    """An explicit tier claims its ids, so a broad tier below skips them."""
    from poe2market.config import Config, CurrencyTier
    from poe2market.collect.jobs import Collector

    cfg = Config(
        contact_email="t@example.com",
        currency_tier=[
            CurrencyTier(name="fast", currencies=["divine", "chaos"],
                         cadence_minutes=2),
            CurrencyTier(name="core", categories=["Currency"],
                         cadence_minutes=10),
        ],
    )
    collector = Collector.__new__(Collector)
    collector.cfg = cfg
    collector._taxonomy = {
        "Currency": [{"id": i, "text": i}
                     for i in ("divine", "chaos", "exalted", "annul")]
    }

    fast = asyncio.run(collector.tier_currencies(cfg.currency_tiers[0]))
    core = asyncio.run(collector.tier_currencies(cfg.currency_tiers[1]))
    assert fast == ["divine", "chaos"]
    assert "divine" not in core and "chaos" not in core
    assert set(core) == {"exalted", "annul"}


# -- junk rejection and the real split ---------------------------------

from poe2market.ggg.trade import dominant_cluster, resolve_market

# Observed live on divine in Forbidden Rites: eight genuine bids alongside
# thirteen junk entries such as "gives 1 exalted, takes 10000 divine".
REAL_BIDS = [230, 200, 180, 162, 160, 160, 160, 140]
JUNK_BIDS = [1, 1, 1, 1, 1, 0.333, 0.1, 0.002, 0.001, 0.001, 0.0001, 0.0001, 0.0001]
REAL_ASKS = [188, 260, 300]
FAT_FINGER_ASK = 100


def test_cluster_survives_junk_being_the_majority():
    """13 junk vs 8 real: rank-based methods land in the junk."""
    kept = dominant_cluster(REAL_BIDS + JUNK_BIDS)
    assert sorted(kept) == sorted(REAL_BIDS)


def test_median_alone_would_have_failed():
    """Guards the reason the cluster step exists."""
    import statistics
    assert statistics.median(sorted(REAL_BIDS + JUNK_BIDS)) <= 1.0


def test_anchor_is_the_closest_ask_bid_pair():
    _, _, anchor = resolve_market([FAT_FINGER_ASK] + REAL_ASKS,
                                  REAL_BIDS + JUNK_BIDS)
    # Closest pair is ask 188 against bid 180.
    assert anchor == pytest.approx(184.0)


def test_fat_finger_ask_is_dropped():
    asks, _, _ = resolve_market([FAT_FINGER_ASK] + REAL_ASKS,
                                REAL_BIDS + JUNK_BIDS, sigmas=1.0)
    assert FAT_FINGER_ASK not in asks


def test_tighter_sigma_never_widens_the_kept_set():
    prev = None
    for sigma in (1.0, 1.5, 2.0, 3.0):
        asks, bids, _ = resolve_market(
            [FAT_FINGER_ASK] + REAL_ASKS, REAL_BIDS + JUNK_BIDS, sigmas=sigma
        )
        size = len(asks) + len(bids)
        if prev is not None:
            assert size >= prev
        prev = size


def test_resolve_market_handles_one_sided_and_empty():
    assert resolve_market([], []) == ([], [], None)
    asks, bids, anchor = resolve_market([100.0, 110.0], [])
    assert bids == [] and anchor is not None


def test_depth_excludes_junk_offers():
    """Stock behind a nonsense price must not inflate reported depth."""
    from poe2market.ggg.trade import MarketBook, Offer
    real = [Offer("h", "divine", "exalted", p, 10) for p in REAL_BIDS]
    junk = [Offer("h", "divine", "exalted", p, 100_000) for p in JUNK_BIDS]
    book = MarketBook.build("divine", "exalted",
                            [Offer("h", "divine", "exalted", 188, 5)], real + junk)
    assert book.bid_depth < 1000       # junk stock excluded
    assert book.n_bids_raw == len(real) + len(junk)


# -- multi-hop arbitrage -----------------------------------------------

from poe2market.analysis.arbitrage import Edge, find_cycles


def test_consistent_market_has_no_arbitrage():
    """Rates that agree with each other must yield no profitable cycle."""
    # 1 divine = 160 exalted, 1 exalted = 10 chaos, so 1 divine = 1600 chaos.
    # Each leg pays a 2% spread, so no loop can profit.
    def spread(r):
        return r * 0.98
    edges = [
        Edge("divine", "exalted", spread(160)), Edge("exalted", "divine", spread(1 / 160)),
        Edge("exalted", "chaos", spread(10)),   Edge("chaos", "exalted", spread(1 / 10)),
        Edge("divine", "chaos", spread(1600)),  Edge("chaos", "divine", spread(1 / 1600)),
    ]
    assert find_cycles(edges, max_hops=4, min_profit_pct=0.5) == []


def test_dislocated_direct_rate_creates_a_cycle():
    """A direct pair drifting from the routed rate is the arbitrage."""
    def spread(r):
        return r * 0.98
    edges = [
        Edge("divine", "exalted", spread(160)), Edge("exalted", "divine", spread(1 / 160)),
        Edge("exalted", "chaos", spread(10)),   Edge("chaos", "exalted", spread(1 / 10)),
        # Direct divine->chaos pays 25% over the routed 1600.
        Edge("divine", "chaos", spread(2000)),  Edge("chaos", "divine", spread(1 / 1600)),
    ]
    cycles = find_cycles(edges, max_hops=3, min_profit_pct=1.0)
    assert cycles, "expected the dislocation to surface"
    assert cycles[0].profit_pct > 1.0


def test_cycle_min_depth_is_the_tightest_leg():
    edges = [Edge("a", "b", 2.0, depth=500), Edge("b", "a", 1.0, depth=7)]
    c = find_cycles(edges, max_hops=2, min_profit_pct=1.0)[0]
    assert c.min_depth == 7


def test_rotations_are_reported_once():
    edges = [Edge("a", "b", 2.0), Edge("b", "c", 2.0), Edge("c", "a", 2.0)]
    cycles = find_cycles(edges, max_hops=3, min_profit_pct=1.0)
    assert len(cycles) == 1


def test_bait_listings_are_not_stored():
    """A chase unique listed at 1 exalted is bait, not a price."""
    from poe2market.ggg.trade import dominant_cluster
    real = [220.0, 240.0, 260.0, 260.0, 300.0]
    bait = [1.0, 1.0, 2.0]
    kept = dominant_cluster(real + bait)
    assert sorted(kept) == sorted(real)
    assert not any(b in kept for b in bait)


def test_unmarketable_pairs_are_identified_by_value_ratio():
    """A Mirror is ~100k Alchs; no direct market exists at that ratio."""
    from poe2market.collect.jobs import PAIR_MAX_VALUE_RATIO
    mirror_in_ex, alch_in_ex = 500_000.0, 0.5
    ratio = mirror_in_ex / alch_in_ex
    assert ratio > PAIR_MAX_VALUE_RATIO

    # Divine against chaos is a normal, tradeable ratio.
    divine_in_ex, chaos_in_ex = 175.0, 29.0
    assert (divine_in_ex / chaos_in_ex) < PAIR_MAX_VALUE_RATIO


def test_pair_plausibility_band_rejects_bait_but_keeps_dislocations():
    """A 1:1 bait rate is rejected; a genuine 2x dislocation survives."""
    from poe2market.collect.jobs import PAIR_PLAUSIBILITY
    implied = 162.0                      # divine -> exalted
    assert not (implied / PAIR_PLAUSIBILITY <= 1.0 <= implied * PAIR_PLAUSIBILITY)
    dislocated = implied * 2.0
    assert implied / PAIR_PLAUSIBILITY <= dislocated <= implied * PAIR_PLAUSIBILITY
