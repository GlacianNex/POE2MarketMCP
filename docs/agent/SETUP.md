# Setup and operations

Use this guide for a first installation or to repair an existing one. Commands
below run from the repository root and use the virtual environment explicitly;
you do not need to activate it. Replace example absolute paths with your own.

## 1. Install and configure

Prerequisites: Python 3.11 or newer, `uv`, internet access to poe.ninja and GGG,
and an MCP client if you want to ask questions through an assistant. Automatic
background installation supports macOS (launchd) and Linux with a systemd user
session. The commands here use a Unix shell; no Windows service installer is
provided.

Check your tools:

```bash
uv --version
python3 --version
```

If `uv` is unavailable, install it using your package manager, or use the Python
alternative below. If you already have the repository, use that checkout.
Otherwise:

```bash
git clone https://github.com/GlacianNex/POE2MarketMCP.git
cd POE2MarketMCP
```

Install into a `.venv` inside this repository:

```bash
uv venv --python 3.11
uv pip install --python .venv/bin/python -e .
.venv/bin/poe2market init
```

Without `uv`, use an installed Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/poe2market init
```

`init` asks for a contact email, included in GGG request headers, and an optional
account handle such as `Name#1234` for public-listing valuation. It writes
`config/config.toml`. No API key, game password, cookie, or Keychain setup is
required. If that file already exists, edit it and continue; `init --force`
overwrites it and is not needed for upgrades.

For noninteractive setup, copy `config/config.example.toml` to
`config/config.toml` only if the latter does not exist, then edit it. Environment
variables alone do not replace the required config file.

The main settings are top-level TOML keys:

```toml
contact_email = "you@example.com"  # replace with your contact address
leagues = ["@current"]            # follow the active challenge league
base_currency = "exalted"
stash_account = ""               # optional: Name#1234
ninja_cadence_minutes = 60
```

Edit existing keys; do not append duplicates. Put all top-level settings
**before any `[section]` or `[[currency_tier]]` header**. TOML keeps subsequent
keys inside the current table even after a blank line.

`@current-hc` selects the hardcore challenge league; an exact league name pins
collection to that economy. `POE2MARKET_CONTACT_EMAIL` and `POE2MARKET_ACCOUNT`
override the corresponding config values. `POE2MARKET_HOME` selects the root
containing `config/` and `data/`; relative database/watchlist paths resolve from
that root. For the service installer, use the standard repository layout: it
always uses the checkout containing the installed source and its `.venv`.
Shell environment overrides are not automatically passed to the installed
service, so persistent email/account settings belong in the config file.

## 2. Collect once and verify

```bash
.venv/bin/poe2market collect --once
.venv/bin/poe2market status
.venv/bin/poe2market price cur:divine
```

The first pass resolves leagues, loads catalog data, collects prices, and runs
maintenance. With the default configuration, look for a recent `ninja` run
with `ok`, a positive number priced, nonzero samples, and the intended league.
Read errors in the output: `collect --once` can exit successfully even when an
individual job failed. Enabling optional scans can make a pass take longer
because the rate limiter waits for budget.

`price` should show a timestamp, source, and unit. If this particular currency
is unavailable, inspect coverage through `market_status` and `search_items`
after connecting. A missing item does not mean the whole collector is broken.

History starts now. Rollups run every 15 minutes by default, and useful trends
need samples across multiple time buckets. Installing the server cannot create
past history or fill gaps while the machine was asleep.

## 3. Connect your MCP client

The client launches the MCP server over **stdio**; there is no HTTP URL or port
to enter. Use these launch settings in your client's local MCP configuration:

```json
{
  "mcpServers": {
    "poe2market": {
      "command": "/absolute/path/to/POE2MarketMCP/.venv/bin/python",
      "args": ["-m", "poe2market.mcp.server"],
      "env": {
        "POE2MARKET_HOME": "/absolute/path/to/POE2MarketMCP"
      }
    }
  }
}
```

This is an example for clients that accept `mcpServers` JSON. In a client with
separate fields, use the same command, arguments, and environment values; do not
paste JSON into a TOML config. Merge the entry with existing servers. Use
absolute paths, not `~`, relative paths, or a shell activation command. Run
`pwd` in the checkout to find its absolute path.

Reload the server or restart the client after changing its launch settings.
Ask it to call `market_status`, then `search_items` for “Divine” and `get_price`
with the returned key. The database path and league should match CLI `status`.
The client can read `poe2market://setup`, `poe2market://tools`, and
`poe2market://state` for instructions and current coverage.

The client keeps its server process alive for the connection. Closing the
client ends that session; it does not stop a separately installed collector.
Running the MCP module directly in a terminal may appear to hang because it
is waiting for protocol input. Use `poe2market status` for a terminal check.

## 4. Keep collection running

After the one-pass check succeeds:

```bash
.venv/bin/poe2market install-daemon
.venv/bin/poe2market status
```

On macOS, this installs a user LaunchAgent that starts at login:

```bash
launchctl list com.igorchernyy.poe2market.collector
tail -n 80 logs/collector.err.log
```

Python logging goes to `collector.err.log`; `collector.log` is stdout and may
be empty. To follow new log entries, use `tail -f logs/collector.err.log`.

On Linux with systemd:

```bash
systemctl --user status poe2market-collector
journalctl --user -u poe2market-collector -n 80
```

This is a user service; running it before login or after logout depends on your
system's user-session configuration. On a system without a supported service
manager, keep `.venv/bin/poe2market collect` running in a terminal.

The collector cannot collect while the machine is asleep or off. It resumes due
jobs after waking and retries failed/empty fetches with backoff capped at five
minutes. Config and watchlist edits reload on a subsequent scheduler tick;
MCP config is cached, so restart the client/server after changing its settings.
Avoid running a foreground collector alongside the installed service.

To stop and remove the service (retaining config and history):

```bash
.venv/bin/poe2market uninstall-daemon
```

## Optional public-listing valuation

Set `stash_account = "Name#1234"` at the top level of `config/config.toml`, then:

```bash
.venv/bin/poe2market stash
.venv/bin/poe2market stash --include-listed
```

The first command values supported currency from your public trade listings.
The second also includes gear at your own asking prices, which are speculative.
These lookups make network requests. They do not read private stash tabs.

In game, make the relevant tab public and give the items a buyout price, either
individually or through the tab price field (for example `~price 5 exalted`).
That is a real public asking price: choose one you intend to list at. Merely
making a tab public without prices is insufficient for this lookup. Log in and
allow indexing to refresh before checking again.

Unlisted holdings and Currency Exchange orders are invisible. A low total is
therefore a valuation of visible listings, not your total account wealth.
Check the account discriminator, league, prices, and indexing if items are missing.

## Optional item watchlists and direct pairs

Basic currency collection needs no watchlist. All bundled files in
`config/watchlists/` are disabled examples; some targets may not match the
current league. To track items:

1. Choose a file, review its targets, and set unwanted targets to `enabled = false`.
2. Set the file's top-level `enabled = true` and choose a cadence.
3. Run `.venv/bin/poe2market watchlists -v` to inspect active lists.
4. Run `.venv/bin/poe2market validate` after the first collection has resolved
   the league, or pass `--league "Exact League Name"`.

Validation spends GGG search budget and checks only enabled lists/targets.
With all lists disabled, “all targets match” does not mean any were tested.
`DEAD` means zero matches, `THIN` means fewer than three, and `ERROR` means the
lookup failed. Loosen filters or disable unsuitable targets before relying on
history. Named uniques use `kind = "unique"`, bases use `kind = "base"` with
optional filters, and `kind = "raw"` accepts a trade query; examples live in the
bundled files. Every enabled item scan spends search/fetch budget on a schedule.

GGG `[[currency_tier]]` sweeps and `pair_currencies` are advanced, disabled
features. They also spend trade budget. `find_multi_step_arbitrage` requires
stored direct pair rates; default poe.ninja collection alone does not populate
them. `suggest-pairs` needs recent two-sided GGG exchange samples, so an empty
report under the default setup is expected. Once you have that data, review its
suggestion and include the base currency in any routing list. Leave these
features disabled if you only need currency prices and history.

## Updating or moving an installation

Before updating, stop the collector, close its MCP client connection, and back
up `config/config.toml` and the database. SQLite can have live `-wal`/`-shm`
sidecars: use a SQLite-aware backup for a live database, or stop all users of
it before copying the database and any remaining sidecars together.

After updating the checkout, reinstall dependencies with
`uv pip install --python .venv/bin/python -e .`, run the one-pass checks, and
reinstall the service. Restart the MCP client. Existing config files are not
migrated by editing the example template: compare settings manually.

In particular, older templates pinned a league and placed
`ninja_cadence_minutes` inside the last currency tier. Set your desired league
and move that cadence key above all table headers in an existing config.

On another machine, clone and install afresh; do not copy `.venv`. Copy your
config and a consistent database backup if you want to preserve history. Update
MCP absolute paths and install the service from the new checkout. Uninstall the
old service before moving its folder. Prefer one collector for one history;
separate databases cannot coordinate GGG budget across machines sharing an IP.
Do not point independent machines at a live SQLite file on a network share.

## Troubleshooting by symptom

- **`uv` or `poe2market` not found:** use the Python install alternative or the
  explicit `.venv/bin/poe2market` path. Run commands from the checkout root.
- **Missing config / wrong database:** run `init` for a fresh checkout and check
  `POE2MARKET_HOME` in both your shell and MCP client. Compare database paths.
- **`init` says config exists:** edit the existing file and continue. `--force`
  replaces your settings.
- **Config edit ignored:** check TOML table placement and environment overrides.
  Restart the MCP connection. Service installs use the source checkout root.
- **No samples / `ninja` errors:** inspect collector output or service logs,
  confirm connectivity and the resolved league, then retry one pass with the
  service stopped. Check per-job status rather than just the shell exit code.
- **HTTP 429 / slow live lookup:** allow the advertised cooldown to expire;
  reduce optional scans and avoid repeated retries. GGG budget is shared by IP.
- **Price missing or history short:** verify the item key and league, then check
  sample counts and timestamps. Disabled watchlists build no item history.
- **MCP does not connect:** verify the absolute interpreter path exists and the
  package is installed there; inspect the client's server error log. Use stdio,
  separate arguments, and an explicit `POE2MARKET_HOME`.
- **Service installed but data stale:** check launchd/systemd status and stderr
  logs, confirm the machine is awake, and reinstall after moving the checkout.
- **Stash empty:** verify `Name#1234`, league, public prices, and indexing; private
  holdings are outside this tool's coverage.
