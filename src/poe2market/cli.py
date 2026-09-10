"""Command line interface: operate the collector and inspect the database."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import load_config, load_watchlists
from .store.db import Store


def _league(args, cfg) -> str:
    """League for a CLI command: explicit flag, else what the collector resolved."""
    if getattr(args, "league", None):
        return args.league
    from .ggg.leagues import cached_leagues

    resolved = cached_leagues(Store(cfg.db_path))
    if resolved:
        return resolved[0]
    concrete = [x for x in cfg.leagues if not x.startswith("@")]
    return concrete[0] if concrete else ""

LAUNCH_LABEL = "com.igorchernyy.poe2market.collector"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def cmd_init(args: argparse.Namespace) -> int:
    """Create config/config.toml interactively — the first thing to run."""
    root = Path(os.environ.get("POE2MARKET_HOME", Path(__file__).resolve().parents[2]))
    cfg_path = root / "config" / "config.toml"
    example = root / "config" / "config.example.toml"

    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} already exists. Re-run with --force to overwrite.")
        return 1
    if not example.exists():
        print(f"Missing template: {example}")
        return 1

    print("POE2 Market MCP — setup\n")
    print("GGG's API policy requires a contact address in the request headers,")
    print("so they can reach you if your traffic causes problems. Use a real one.")
    email = input("  Contact email: ").strip()
    while "@" not in email:
        email = input("  Contact email (must be valid): ").strip()

    print("\nYour PoE2 account handle enables stash valuation (optional).")
    print("It's the 'Name#1234' form — find it on your profile. Leave blank to skip.")
    account = input("  Account handle (Name#1234): ").strip()

    text = example.read_text()
    import re
    text = re.sub(r'contact_email\s*=\s*".*"', f'contact_email = "{email}"', text)
    text = re.sub(r'stash_account\s*=\s*".*"', f'stash_account = "{account}"', text)
    cfg_path.write_text(text)

    print(f"\n  ✓ wrote {cfg_path}")
    print("  Next: `poe2market install-daemon` to start collecting prices,")
    print("        then `poe2market status`.")
    if not account:
        print("  (Set stash_account later to enable `poe2market stash`.)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    st = Store(cfg.db_path).stats()
    print(f"database   {cfg.db_path}  ({st['db_bytes'] / 1024:.0f} KiB)")
    print(f"items      {st['item']:,} catalogued")
    print(f"samples    {st['price_sample']:,} raw   "
          f"{st['price_hourly']:,} hourly   {st['price_daily']:,} daily")
    print(f"window     {st['earliest_sample']} -> {st['latest_sample']}")
    print(f"leagues    {', '.join(lg['name'] for lg in st['leagues']) or '(none yet)'}")
    print(f"stash      {st['stash_snapshot']:,} snapshots")
    print()
    with Store(cfg.db_path).conn() as c:
        rows = c.execute(
            "SELECT job, status, items_priced, started_at, detail "
            "FROM collector_run ORDER BY started_at DESC LIMIT 8"
        ).fetchall()
    if rows:
        print("recent collector runs:")
        for r in rows:
            note = f"  {r['detail']}" if r["detail"] else ""
            print(f"  {r['started_at']}  {r['status']:<8} {r['job']:<28} "
                  f"{r['items_priced']:>4} priced{note}")
    else:
        print("no collector runs yet — start it with `poe2market collect --once`")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from .collect.daemon import CollectorDaemon

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    daemon = CollectorDaemon(cfg)
    asyncio.run(daemon.run(once=args.once, job_filter=args.job))
    return 0


def cmd_price(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    league = _league(args, cfg)
    row = store.latest(args.item_key, league)
    if not row:
        print(f"no recorded price for {args.item_key!r} in {league!r}")
        return 1
    base_cur, listed_cur = row.get("base_currency"), row.get("currency")
    price = row.get("price_base")
    print(f"{row.get('label') or args.item_key}  ({league})")
    print(f"  as of      {row['ts']}   source: {row.get('source')}")
    print(f"  price      {price:,.4f} {base_cur}" if price
          else f"  price      - (no {base_cur} rate for {listed_cur} yet)")

    if row.get("source") == "exchange":
        bid, ask = row.get("low"), row.get("high")
        spread = ((ask - bid) / price * 100.0) if (bid and ask and price) else None
        print(f"  bid / ask  {bid or '-'} / {ask or '-'}  ({base_cur})")
        print(f"  spread     {spread:.1f}%" if spread is not None
              else "  spread     n/a (one-sided book)")
        print(f"  depth      {row.get('total_stock')}")
    else:
        f = lambda v: f"{v:,.2f}" if v is not None else "-"
        print(f"  asking     {f(row.get('low'))} / {f(row.get('median'))} / "
              f"{f(row.get('high'))}  (low/median/high, in {listed_cur})")
        print("  spread     n/a — ask-side listings only, no bid side")
    print(f"  listings   {row.get('n_listings')}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    cfg = load_config()
    league = _league(args, cfg)
    rows = Store(cfg.db_path).history(
        args.item_key, league, days=args.days, resolution=args.resolution
    )
    if not rows:
        print("no history yet (rollups run every "
              f"{cfg.rollup_cadence_minutes}m; history starts when the "
              "collector first ran)")
        return 1
    print(f"{'bucket':<24}{'open':>12}{'high':>12}{'low':>12}{'close':>12}{'n':>5}")
    for r in rows:
        f = lambda v: f"{v:,.2f}" if v is not None else "-"
        print(f"{r['bucket']:<24}{f(r['open']):>12}{f(r['high']):>12}"
              f"{f(r['low']):>12}{f(r['close']):>12}{r['samples']:>5}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Run every watch target once and report how many listings it matches.

    An over-constrained query is the quiet failure mode here: it never errors,
    it just records an empty series for as long as you leave it running. This
    surfaces that before you lose a week of history to it.
    """
    from .ggg.client import GGGClient
    from .ggg.trade import TradeAPI, build_query
    from .ratelimit import RateLimiter

    cfg = load_config()
    league = _league(args, cfg)
    lists = load_watchlists(cfg)

    async def run() -> int:
        limiter = RateLimiter(str(cfg.db_path))
        problems = 0
        async with GGGClient(cfg.user_agent, limiter) as client:
            api = TradeAPI(client)
            for wl in lists:
                print(f"\n{wl.name}  (every {wl.cadence_minutes}m)")
                for t in wl.active_targets():
                    try:
                        _id, _ids, total = await api.search(league, build_query(t))
                    except Exception as exc:
                        print(f"  ERROR  {t.key:<32} {str(exc)[:60]}")
                        problems += 1
                        continue
                    if total == 0:
                        flag, problems = "DEAD ", problems + 1
                    elif total < 3:
                        flag = "THIN "
                    else:
                        flag = "ok   "
                    print(f"  {flag}  {t.key:<32} {total:>5} listings")
        return problems

    problems = asyncio.run(run())
    print()
    if problems:
        print(f"{problems} target(s) match nothing — they will record empty "
              f"series. Loosen the filters or disable them.")
        return 1
    print("all targets match live listings")
    return 0


def cmd_suggest_pairs(args: argparse.Namespace) -> int:
    """Recommend a `pair_currencies` set from collected liquidity.

    Direct pair sweeps only pay off for currencies that actually trade on both
    sides and sit within a sane value ratio of each other. Guessing that list
    wastes requests on dead pairs; measuring it does not.
    """
    from .collect.jobs import PAIR_MAX_VALUE_RATIO

    cfg = load_config()
    league = _league(args, cfg)
    rows = Store(cfg.db_path).liquidity_report(league, since_minutes=args.window)
    if not rows:
        print(f"no exchange samples for {league!r} in the last {args.window}m")
        return 1

    viable = [
        r for r in rows
        if r["two_sided_samples"] >= 1
        and (r["offers"] or 0) >= args.min_offers
        and r["price"]
    ]
    print(f"{league}: {len(rows)} currencies priced, "
          f"{len(viable)} with two-sided liquidity\n")
    print(f"{'currency':<22}{'price(ex)':>12}{'offers':>8}{'2-sided':>9}{'samples':>9}")
    print("-" * 62)
    for r in rows[: args.top]:
        mark = "  <-" if r in viable else ""
        print(f"{r['currency_id']:<22}{r['price']:>12.3f}{(r['offers'] or 0):>8.0f}"
              f"{r['two_sided_samples']:>9}{r['samples']:>9}{mark}")

    if not viable:
        print("\nNothing has two-sided liquidity yet; let the collector run longer.")
        return 1

    # Drop anything too far from the median value to trade against the rest.
    prices = sorted(r["price"] for r in viable)
    mid = prices[len(prices) // 2]
    keep = [
        r for r in viable
        if mid / PAIR_MAX_VALUE_RATIO <= r["price"] <= mid * PAIR_MAX_VALUE_RATIO
    ]
    dropped = [r["currency_id"] for r in viable if r not in keep]

    ids = [r["currency_id"] for r in keep][:10]
    print(f"\nsuggested pair_currencies ({len(ids)}):")
    print("  pair_currencies = [" + ", ".join(f'"{i}"' for i in ids) + "]")
    if dropped:
        print(f"\nexcluded on value ratio (no realistic direct market): "
              f"{', '.join(dropped)}")
    return 0


def cmd_watchlists(args: argparse.Namespace) -> int:
    cfg = load_config()
    for w in load_watchlists(cfg):
        print(f"{w.name:<18} priority {w.priority:<3} every {w.cadence_minutes:>4}m  "
              f"{len(w.active_targets())} targets")
        if args.verbose:
            for t in w.active_targets():
                print(f"    {t.key:<30} {t.kind:<9} {t.describe()}")
    print()
    print("currency tiers:")
    for t in cfg.currency_tiers:
        print(f"  {t.name:<14} every {t.cadence_minutes:>4}m  {t.categories}")
    return 0


def cmd_stash(args: argparse.Namespace) -> int:
    """Snapshot and value an account's public listings. No login required."""
    from .collect.stash import snapshot_public_listings

    cfg = load_config()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    league = _league(args, cfg)
    account = args.account or cfg.stash_account
    result = asyncio.run(
        snapshot_public_listings(
            cfg, Store(cfg.db_path), league, account,
            include_listed_prices=args.include_listed,
        )
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1

def _systemd_unit(root: Path, python: Path) -> str:
    return f"""[Unit]
Description=POE2 market collector
After=network-online.target

[Service]
Type=simple
ExecStart={python} -m poe2market.collect.daemon
WorkingDirectory={root}
Environment=POE2MARKET_HOME={root}
Restart=on-failure
RestartSec=60
Nice=5

[Install]
WantedBy=default.target
"""


def cmd_install_daemon(args: argparse.Namespace) -> int:
    """Install the collector as a user service so it survives reboots.

    launchd on macOS, systemd --user elsewhere. Both are *user* services
    deliberately: a system service would run as root, and the account
    credential lives in the user's keyring.
    """
    root = Path(__file__).resolve().parents[2]
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        print(f"no venv python at {python}", file=sys.stderr)
        return 1

    logs = root / "logs"
    logs.mkdir(exist_ok=True)

    if sys.platform != "darwin":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "poe2market-collector.service"
        unit.write_text(_systemd_unit(root, python))
        print(f"wrote {unit}")
        if args.dry_run:
            print("dry run: not enabling. Enable it with:")
            print("  systemctl --user daemon-reload")
            print("  systemctl --user enable --now poe2market-collector")
            return 0
        for cmd in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "poe2market-collector"],
        ):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"{' '.join(cmd)} failed: {proc.stderr.strip()}",
                      file=sys.stderr)
                return 1
        print("collector enabled; logs: journalctl --user -u poe2market-collector -f")
        return 0

    plist = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [
            str(python), "-m", "poe2market.collect.daemon",
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"POE2MARKET_HOME": str(root)},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        # Restart at most every 60s so a crash loop cannot hammer the API.
        "ThrottleInterval": 60,
        "StandardOutPath": str(logs / "collector.log"),
        "StandardErrorPath": str(logs / "collector.err.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
    }

    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    print(f"wrote {path}")

    if args.dry_run:
        print("dry run: not loading. Load it with:")
        print(f"  launchctl bootstrap gui/{os.getuid()} {path}")
        return 0

    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"],
        capture_output=True,
    )
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"launchctl bootstrap failed: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"loaded {LAUNCH_LABEL}; logs at {logs / 'collector.log'}")
    return 0


def cmd_uninstall_daemon(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "poe2market-collector"],
            capture_output=True,
        )
        unit = Path.home() / ".config/systemd/user/poe2market-collector.service"
        if unit.exists():
            unit.unlink()
            print(f"removed {unit}")
        print("collector disabled")
        return 0

    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"],
        capture_output=True,
    )
    path = _plist_path()
    if path.exists():
        path.unlink()
        print(f"removed {path}")
    print("collector unloaded")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="poe2market", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="Create config.toml interactively (run this first)")
    i.add_argument("--force", action="store_true", help="Overwrite existing config")
    i.set_defaults(func=cmd_init)

    sub.add_parser("status", help="Database and collector summary").set_defaults(
        func=cmd_status
    )

    c = sub.add_parser("collect", help="Run collection jobs")
    c.add_argument("--once", action="store_true", help="Run due jobs once, then exit")
    c.add_argument("--job", default=None, help="Only jobs whose name contains this")
    c.set_defaults(func=cmd_collect)

    pr = sub.add_parser("price", help="Latest recorded price for an item")
    pr.add_argument("item_key")
    pr.add_argument("--league", default=None)
    pr.set_defaults(func=cmd_price)

    h = sub.add_parser("history", help="OHLC candles for an item")
    h.add_argument("item_key")
    h.add_argument("--league", default=None)
    h.add_argument("--days", type=int, default=30)
    h.add_argument("--resolution", choices=["auto", "hourly", "daily"], default="auto")
    h.set_defaults(func=cmd_history)


    v = sub.add_parser(
        "validate", help="Check every watch target matches real listings"
    )
    v.add_argument("--league", default=None)
    v.set_defaults(func=cmd_validate)

    sp = sub.add_parser(
        "suggest-pairs",
        help="Recommend pair_currencies from measured two-sided liquidity",
    )
    sp.add_argument("--league", default=None)
    sp.add_argument("--window", type=int, default=180, help="Minutes to consider")
    sp.add_argument("--min-offers", type=float, default=3.0)
    sp.add_argument("--top", type=int, default=20)
    sp.set_defaults(func=cmd_suggest_pairs)

    w = sub.add_parser("watchlists", help="Show configured scan targets")
    w.add_argument("-v", "--verbose", action="store_true")
    w.set_defaults(func=cmd_watchlists)

    st = sub.add_parser(
        "stash", help="Snapshot & value an account's public listings (no login)"
    )
    st.add_argument("--account", default=None,
                    help='Account handle, e.g. "Name#1234" (default: config)')
    st.add_argument("--league", default=None)
    st.add_argument("--include-listed", action="store_true",
                    help="Include self-set gear asking prices (speculative)")
    st.set_defaults(func=cmd_stash)

    i = sub.add_parser("install-daemon", help="Install the launchd collector agent")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=cmd_install_daemon)

    sub.add_parser(
        "uninstall-daemon", help="Remove the launchd collector agent"
    ).set_defaults(func=cmd_uninstall_daemon)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
