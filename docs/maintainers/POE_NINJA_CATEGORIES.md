# poe.ninja PoE2 categories (reference)

Base: `https://poe.ninja/poe2/api/economy`
Needs a browser-like `User-Agent` and `Referer: https://poe.ninja/poe2/economy`
(some non-browser requests 404). CDN-cached ~30 min (`max-age=1800`);
underlying data refreshes ~hourly. Prices are denominated in **Divine Orbs**
(`primaryValue`); convert via the exalted rate from the Currency payload.

Discovered by exhaustive probing on 2026-09-10 in *Forbidden Rites*. Re-probe
each league — a category with 0 lines simply isn't live yet.

## Exchange (currency-like) — `/exchange/current/overview?league={L}&type={T}`

These are what `NinjaClient.CURRENCY_TYPES` pulls. **All 13** must stay covered;
omens (worth hundreds of ex) live under **Ritual**, not a category of their own.

| type | ~lines | holds |
|---|---|---|
| `Currency` | 52 | orbs; the only category with the exalted rate |
| `Ritual` | 39 | **omens** (Whittling, Chance, Annulment — high value) |
| `Fragments` | 26 | breach/map fragments, splinters |
| `Essences` | 80 | essences (all tiers) |
| `Runes` | 145 | runes + soul cores overflow |
| `Breach` | 28 | breach catalysts, splinters |
| `Delirium` | 26 | distilled emotions, liquids |
| `Expedition` | 18 | expedition currency, logbooks |
| `SoulCores` | 52 | soul cores |
| `Abyss` | 15 | gnawed bones, abyssal items |
| `Idols` | 35 | idols |
| `UncutGems` | 42 | uncut skill/support/spirit gems, by level |
| `Verisium` | 24 | verisium-line currency |

## Item (uniques) — `/stash/current/item/overview?league={L}&type={T}`

**Not yet pulled.** A separate endpoint for named uniques, priced by name.
Wire these in to value unique items in a stash (currently only their own
listed price is used).

| type | ~lines |
|---|---|
| `UniqueWeapons` | 143 |
| `UniqueArmours` | 424 |
| `UniqueAccessories` | 86 |
| `UniqueFlasks` | 6 |
| `UniqueCharms` | 12 |
| `UniqueJewels` | 14 |
| `UniqueSanctumRelics` | 5 |
| `UniqueTablets` | 9 |
| `PrecursorTablets` | 23 |

## Payload shape

```
{ "items": [ {"id","name","category",...} ],   # id -> display name
  "lines": [ {"id","primaryValue","volumePrimaryValue","maxVolumeCurrency",
              "maxVolumeRate","sparkline"} ] }   # id -> price in divine
```

`primaryValue` is the price in Divine Orbs. `volumePrimaryValue` is trade
volume (Divine had 73,724 — the in-game Currency Exchange, not the trade site).
