"""Resolving which league is 'the current one'.

There is no date metadata for PoE2 leagues: ``pathofexile.com/api/leagues``
serves PoE1 only, and ``api.pathofexile.com/league`` needs OAuth. What
``trade2/data/leagues`` does give is an ordering, and GGG lists the active
challenge league first:

    Forbidden Rites, HC Forbidden Rites, Runes of Aldur, HC Runes of Aldur,
    Standard, Hardcore

So "current" is the first entry that is neither permanent nor a variant of
another league. Resolution is cached in the database, both to avoid an API call
on every default-league lookup and so that a temporary API failure cannot
silently redirect collection into the wrong league.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: Tokens accepted in ``leagues`` in config.toml.
CURRENT = "@current"
CURRENT_HC = "@current-hc"

PERMANENT = {
    "Standard", "Hardcore",
    "SSF Standard", "SSF Hardcore",
    "Ruthless", "HC Ruthless", "SSF Ruthless", "HC SSF Ruthless",
}

#: Prefixes marking a variant of some other league rather than its own economy.
VARIANT_PREFIXES = ("HC ", "SSF ", "HC SSF ", "R ", "Ruthless ")


def is_permanent(league_id: str) -> bool:
    return league_id in PERMANENT


def is_variant(league_id: str) -> bool:
    return league_id.startswith(VARIANT_PREFIXES) or "Ruthless" in league_id


def pick_current(leagues: list[dict[str, Any]], hardcore: bool = False) -> str | None:
    """Return the id of the active challenge league, or None.

    ``leagues`` is the raw ``trade2/data/leagues`` result.
    """
    ids = [L.get("id", "") for L in leagues if L.get("id")]

    softcore = next(
        (i for i in ids if not is_permanent(i) and not is_variant(i)), None
    )
    if not hardcore:
        return softcore
    if not softcore:
        return None
    # The hardcore counterpart is the same name with an HC prefix, when listed.
    hc = f"HC {softcore}"
    return hc if hc in ids else None


def expand_tokens(
    configured: list[str], leagues: list[dict[str, Any]]
) -> list[str]:
    """Replace ``@current`` / ``@current-hc`` with concrete league ids.

    An empty configuration also means "the current league", so a fresh install
    collects something sensible without being edited.
    """
    if not configured:
        configured = [CURRENT]

    out: list[str] = []
    for entry in configured:
        if entry == CURRENT:
            resolved = pick_current(leagues, hardcore=False)
        elif entry == CURRENT_HC:
            resolved = pick_current(leagues, hardcore=True)
        else:
            out.append(entry)
            continue

        if resolved:
            log.info("resolved %s -> %r", entry, resolved)
            out.append(resolved)
        else:
            log.warning("could not resolve %s from the league list", entry)

    # Preserve order, drop duplicates.
    return list(dict.fromkeys(out))


RESOLVED_KEY = "resolved_leagues"


async def resolve_leagues(cfg, api, store) -> list[str]:
    """Expand configured tokens to concrete league ids and cache the result.

    Falls back to the last cached resolution if the API call fails, so a
    transient outage cannot silently redirect collection into a wrong league or
    stall the collector entirely.
    """
    try:
        listing = await api.leagues()
        resolved = expand_tokens(cfg.leagues, listing)
        if resolved:
            store.set_meta(RESOLVED_KEY, ",".join(resolved))
            return resolved
        log.warning("league resolution produced nothing; falling back to cache")
    except Exception:
        log.exception("could not fetch the league list; falling back to cache")

    cached = store.get_meta(RESOLVED_KEY)
    if cached:
        return [x for x in cached.split(",") if x]
    # Last resort: whatever is literally in the config, tokens dropped.
    return [x for x in cfg.leagues if not x.startswith("@")]


def cached_leagues(store) -> list[str]:
    """Concrete leagues resolved by the collector, for read-only callers."""
    cached = store.get_meta(RESOLVED_KEY)
    return [x for x in (cached or "").split(",") if x]
