"""Multi-hop arbitrage over stored direct pair rates.

Pure computation: no API calls. The expensive half is collecting direct pair
quotes (N requests for N currencies, since one request prices 10 pairs); once
they are in the database, searching for profitable cycles is free and can be
re-run as often as anyone likes.

Why direct quotes are required
------------------------------
Every other price in this system is quoted against one base currency. Build a
cycle out of base-relative quotes and each leg pays the bid/ask spread, so the
product of rates is always below 1 — a synthetic cycle can never be profitable
by construction. Genuine multi-hop arbitrage exists only where the market's own
direct chaos->divine rate has drifted away from chaos->exalted->divine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Edge:
    """One directed hop: give ``frm``, receive ``rate`` units of ``to``."""

    frm: str
    to: str
    rate: float
    depth: int = 0
    n_offers: int = 0
    ts: str | None = None


@dataclass
class Cycle:
    """A closed loop of trades and what it returns."""

    path: list[str]
    edges: list[Edge] = field(default_factory=list)

    @property
    def gain(self) -> float:
        """Multiplier on the starting amount. 1.05 == 5% profit."""
        total = 1.0
        for e in self.edges:
            total *= e.rate
        return total

    @property
    def profit_pct(self) -> float:
        return (self.gain - 1.0) * 100.0

    @property
    def min_depth(self) -> int:
        """The tightest leg bounds how much can actually be pushed through."""
        return min((e.depth for e in self.edges), default=0)

    def describe(self) -> str:
        return " -> ".join(self.path)


def build_graph(edges: Iterable[Edge]) -> dict[str, dict[str, Edge]]:
    """Keep the most favourable edge per ordered pair."""
    graph: dict[str, dict[str, Edge]] = {}
    for e in edges:
        if e.rate <= 0:
            continue
        best = graph.setdefault(e.frm, {}).get(e.to)
        if best is None or e.rate > best.rate:
            graph[e.frm][e.to] = e
    return graph


def find_cycles(
    edges: Iterable[Edge],
    *,
    max_hops: int = 4,
    min_profit_pct: float = 1.0,
    start_currencies: list[str] | None = None,
) -> list[Cycle]:
    """Find profitable closed loops, best first.

    Depth-first over a graph of at most a few dozen currencies, bounded by
    ``max_hops``. That is small enough that exhaustive search beats anything
    cleverer, and unlike Bellman-Ford it enumerates *every* profitable cycle
    rather than proving one exists.

    Costs are already embedded: each edge is a real executable rate that has
    paid its own spread, so a product above 1 is genuine profit rather than an
    artefact of mid-price arithmetic.
    """
    graph = build_graph(edges)
    starts = start_currencies or list(graph)
    found: list[Cycle] = []
    seen: set[tuple[str, ...]] = set()

    def walk(start: str, node: str, path: list[str], used: list[Edge]) -> None:
        if len(used) >= max_hops:
            return
        for nxt, edge in graph.get(node, {}).items():
            if nxt == start:
                cycle = Cycle(path=path + [start], edges=used + [edge])
                if cycle.profit_pct >= min_profit_pct:
                    # Rotate to a canonical form so A->B->A and B->A->B are
                    # not reported as two separate opportunities.
                    ring = tuple(cycle.path[:-1])
                    lowest = ring.index(min(ring))
                    key = ring[lowest:] + ring[:lowest]
                    if key not in seen:
                        seen.add(key)
                        found.append(cycle)
                continue
            if nxt in path:
                continue
            walk(start, nxt, path + [nxt], used + [edge])

    for start in starts:
        walk(start, start, [start], [])

    return sorted(found, key=lambda c: c.profit_pct, reverse=True)


def synthetic_rate(
    graph: dict[str, dict[str, Edge]], frm: str, to: str, via: str
) -> float | None:
    """Rate achieved by routing ``frm -> via -> to``, for comparison."""
    a = graph.get(frm, {}).get(via)
    b = graph.get(via, {}).get(to)
    if not a or not b:
        return None
    return a.rate * b.rate


def direct_vs_routed(
    edges: Iterable[Edge], base: str
) -> list[dict[str, object]]:
    """Where the direct rate beats routing through the base currency.

    This is the cheap, one-hop version of the same idea, and usually the more
    actionable one: two trades instead of four, and the dislocation is easier
    to reason about.
    """
    graph = build_graph(edges)
    out: list[dict[str, object]] = []
    for frm, targets in graph.items():
        if frm == base:
            continue
        for to, direct in targets.items():
            if to == base:
                continue
            routed = synthetic_rate(graph, frm, to, base)
            if not routed:
                continue
            edge_pct = (direct.rate - routed) / routed * 100.0
            out.append(
                {
                    "from": frm,
                    "to": to,
                    "direct_rate": direct.rate,
                    "routed_rate": routed,
                    "advantage_pct": edge_pct,
                    "depth": direct.depth,
                }
            )
    return sorted(out, key=lambda d: d["advantage_pct"], reverse=True)
