"""Configuration: global settings plus declarative watchlists.

A *watchlist* is a named group of scan targets with its own cadence and
priority, so the rate budget can be steered at what matters right now. At
league start you might run ``league-start.toml`` every 20 minutes and let
``chase-uniques.toml`` idle; a month in, you flip that around.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DEFAULT_ROOT = Path(
    os.environ.get("POE2MARKET_HOME", Path(__file__).resolve().parents[2])
)


class CurrencyTier(BaseModel):
    """A slice of the currency universe with its own cadence.

    A full sweep of all ~800 exchangeable items costs ~160 requests against a
    30-per-5-minute budget, so sweeping everything at core cadence is not
    possible. Tiering spends the budget where it matters: core orbs every few
    minutes, the long tail a few times a day.

    ``categories`` are ids from ``/api/trade2/data/static`` (``Currency``,
    ``Fragments``, ``Essences``, ...). ``"*"`` means "everything not already
    claimed by an earlier tier".
    """

    name: str
    categories: list[str] = Field(default_factory=list)
    currencies: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit exchange ids. Use for a small fast lane; a tier of 10 "
            "or fewer costs just 2 requests per pass and can run every minute."
        ),
    )
    cadence_minutes: int = Field(default=60, ge=1)
    enabled: bool = True


class WatchTarget(BaseModel):
    """One thing to price on a schedule.

    Three ways to say what to price, in increasing order of power:

    * ``kind="currency"`` — an exchange id (``divine``). Priced in bulk via the
      exchange endpoint, so these are nearly free.
    * ``kind="unique"`` / ``kind="base"`` — a name or base type, optionally
      narrowed by ``filters``. Costs one search + one fetch per sample.
    * ``kind="raw"`` — a full trade2 query body, for anything the shorthands
      cannot express (specific mod rolls, rune sockets, quality bands).
    """

    key: str = Field(description="Stable id; becomes the series key in the DB.")
    label: str = ""
    kind: Literal["currency", "unique", "base", "raw"] = "unique"

    name: str | None = Field(default=None, description="Unique item name.")
    type: str | None = Field(default=None, description="Base type, e.g. 'Siphoning Wand'.")
    currency_id: str | None = Field(default=None, description="Exchange id for kind=currency.")

    filters: dict[str, Any] = Field(default_factory=dict)
    raw_query: dict[str, Any] | None = None

    sample_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Listings to price per sample. 10 == one fetch call.",
    )
    cadence_minutes: int | None = Field(
        default=None, description="Overrides the watchlist cadence."
    )
    enabled: bool = True

    @field_validator("label")
    @classmethod
    def _default_label(cls, v: str, info) -> str:
        return v or info.data.get("key", "")

    def describe(self) -> str:
        if self.kind == "currency":
            return self.currency_id or self.key
        return self.name or self.type or self.key


class Watchlist(BaseModel):
    """A named, independently-scheduled group of targets."""

    name: str
    description: str = ""
    enabled: bool = True
    cadence_minutes: int = Field(default=60, ge=1)
    priority: int = Field(
        default=5,
        description="Higher wins when the rate budget is contended.",
    )
    targets: list[WatchTarget] = Field(default_factory=list, alias="target")

    model_config = {"populate_by_name": True}

    def active_targets(self) -> list[WatchTarget]:
        return [t for t in self.targets if t.enabled]


class Config(BaseModel):
    """Top-level settings, loaded from ``config/config.toml``."""

    contact_email: str = Field(
        description="Required by GGG's API policy; sent in the User-Agent."
    )
    leagues: list[str] = Field(
        default_factory=list,
        description="Leagues to collect. Empty means 'the current temp leagues'.",
    )
    realm: str = "poe2"

    db_path: Path = Path("data/market.db")
    currency_cadence_minutes: int = Field(default=10, ge=1)
    rollup_cadence_minutes: int = Field(default=15, ge=1)
    raw_retention_days: int = Field(default=14, ge=1)

    # Reference currency that prices are normalised to for charting.
    base_currency: str = "exalted"

    # How far from the real split (the closest ask/bid pair) a listing may sit
    # before it is discarded as stale, mistyped, or junk. Measured in robust
    # standard deviations in log space. Lower is stricter: 1.0 keeps only the
    # tight core, 2.0 tolerates a wider but still plausible market.
    outlier_sigmas: float = Field(default=1.0, ge=0.5, le=5.0)

    currency_tiers: list[CurrencyTier] = Field(
        default_factory=list, alias="currency_tier"
    )
    pair_currencies: list[str] = Field(default_factory=list)
    pair_cadence_minutes: int = Field(default=15, ge=1)
    # poe.ninja's underlying data refreshes ~hourly, so polling faster just
    # re-reads the same CDN-cached response.
    ninja_cadence_minutes: int = Field(default=60, ge=5)

    # Your account handle, e.g. "Name#1234" — WITH the discriminator.
    # This is all that is needed to read your public listings; there is no
    # login. Only publicly-listed items are visible (mark tabs public in game).
    stash_account: str = ""

    watchlist_dir: Path = Path("config/watchlists")
    log_level: str = "INFO"

    model_config = {"populate_by_name": True}

    @property
    def user_agent(self) -> str:
        # GGG asks for a descriptive UA with a contact address.
        return f"POE2MarketMCP/0.1 (+https://github.com/local; contact: {self.contact_email})"


def _resolve(root: Path, p: Path) -> Path:
    return p if p.is_absolute() else (root / p)


def load_config(root: Path | None = None) -> Config:
    """Load ``config/config.toml``, resolving paths relative to the project."""
    root = root or DEFAULT_ROOT
    path = root / "config" / "config.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run `poe2market init` to create one."
        )
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    env_email = os.environ.get("POE2MARKET_CONTACT_EMAIL")
    if env_email:
        data["contact_email"] = env_email
    env_account = os.environ.get("POE2MARKET_ACCOUNT")
    if env_account:
        data["stash_account"] = env_account

    cfg = Config(**data)
    cfg.db_path = _resolve(root, cfg.db_path)
    cfg.watchlist_dir = _resolve(root, cfg.watchlist_dir)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg


def load_watchlists(cfg: Config) -> list[Watchlist]:
    """Load every ``*.toml`` in the watchlist directory, highest priority first."""
    if not cfg.watchlist_dir.exists():
        return []
    lists: list[Watchlist] = []
    for path in sorted(cfg.watchlist_dir.glob("*.toml")):
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        data.setdefault("name", path.stem)
        lists.append(Watchlist(**data))
    return sorted(
        [w for w in lists if w.enabled], key=lambda w: w.priority, reverse=True
    )
