"""The collector daemon.

History cannot be backfilled — GGG serves only current listings — so this
process running continuously is what makes per-league graphs possible at all.
It is deliberately dull: a cadence-driven loop with no external scheduler,
restartable at any point, and safe to run alongside the MCP server because the
rate-limit ledger and the database are both shared.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable

from ..config import DEFAULT_ROOT, Config, load_config, load_watchlists
from ..ggg.client import GGGClient
from ..ggg.leagues import resolve_leagues
from ..ggg.trade import TradeAPI
from ..ratelimit import RateLimiter
from ..store.db import Store, utcnow
from .jobs import Collector

log = logging.getLogger("poe2market.collector")

#: Retry pacing for a failing job. Short and capped so recovery after an
#: outage, sleep, or dropped connection happens quickly rather than waiting
#: out the job's full cadence.
RETRY_BASE = timedelta(seconds=30)
RETRY_MAX = timedelta(minutes=5)

#: A gap this much longer than a job's cadence means the machine was asleep,
#: offline, or the daemon was stopped — worth logging as a catch-up.
CATCHUP_FACTOR = 2


@dataclass
class ScheduledJob:
    name: str
    interval: timedelta
    run: Callable[[], Awaitable[Any]]
    next_run: Any = None
    failures: int = 0
    last_result: Any = field(default=None, repr=False)

    def due(self, now) -> bool:
        return self.next_run is None or now >= self.next_run

    def reschedule(self, now) -> None:
        """Pick the next run time.

        On failure this retries *sooner*, not later. Exponential backoff on the
        full cadence is wrong for an outage: a 30-minute job that fails while
        the network is down would wait hours after it returns. Instead a failing
        job polls on a short, capped interval so it resumes within a minute or
        two of connectivity coming back, then returns to its normal cadence on
        the first success.
        """
        if self.failures:
            delay = min(RETRY_BASE * (2 ** (self.failures - 1)), RETRY_MAX)
            self.next_run = now + min(delay, self.interval)
        else:
            self.next_run = now + self.interval


def build_jobs(cfg: Config, collector: Collector) -> list[ScheduledJob]:
    jobs: list[ScheduledJob] = []

    for league in cfg.leagues:
        # poe.ninja in-game exchange data — the accurate currency source.
        jobs.append(
            ScheduledJob(
                name=f"{league}/ninja",
                interval=timedelta(minutes=cfg.ninja_cadence_minutes),
                run=_bind(collector.sweep_ninja_currency, league),
            )
        )
        for tier in cfg.currency_tiers:
            if not tier.enabled:
                continue
            jobs.append(
                ScheduledJob(
                    name=f"{league}/currency:{tier.name}",
                    interval=timedelta(minutes=tier.cadence_minutes),
                    run=_bind(collector.sweep_currency, league, tier),
                )
            )
        if cfg.pair_currencies:
            jobs.append(
                ScheduledJob(
                    name=f"{league}/pairs",
                    interval=timedelta(minutes=cfg.pair_cadence_minutes),
                    run=_bind(collector.sweep_pairs, league, cfg.pair_currencies),
                )
            )

        for wl in load_watchlists(cfg):
            jobs.append(
                ScheduledJob(
                    name=f"{league}/watchlist:{wl.name}",
                    interval=timedelta(minutes=wl.cadence_minutes),
                    run=_bind(collector.scan_watchlist, league, wl),
                )
            )

    jobs.append(
        ScheduledJob(
            name="maintain",
            interval=timedelta(minutes=cfg.rollup_cadence_minutes),
            run=_bind_sync(collector.maintain),
        )
    )
    return jobs


def watchlist_fingerprint(cfg: Config) -> tuple:
    """Cheap signature of the watchlist files on disk.

    Watchlists get edited exactly when the daemon is least convenient to
    restart — league start, while prices move. Polling mtimes lets a new or
    edited list take effect on the next tick instead.
    """
    paths = sorted(cfg.watchlist_dir.glob("*.toml")) if cfg.watchlist_dir.exists() else []
    sig = [(p.name, p.stat().st_mtime_ns) for p in paths]
    # config.toml too, so tier cadences can be retuned mid-league.
    main = DEFAULT_ROOT / "config" / "config.toml"
    if main.exists():
        sig.append((main.name, main.stat().st_mtime_ns))
    return tuple(sig)


def _is_soft_failure(result: Any) -> bool:
    """True when a job returned without raising but achieved nothing."""
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return True
    # Jobs report how much they collected under one of these keys.
    for key in ("priced", "pairs", "written"):
        if key in result:
            return not result[key]
    return False


def _bind(fn, *args):
    async def runner():
        return await fn(*args)
    return runner


def _bind_sync(fn, *args):
    async def runner():
        return await asyncio.to_thread(fn, *args)
    return runner


class CollectorDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.limiter = RateLimiter(str(cfg.db_path))
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        log.info("stop requested, finishing current job")
        self._stop.set()

    async def run(self, once: bool = False, job_filter: str | None = None) -> None:
        async with GGGClient(self.cfg.user_agent, self.limiter) as client:
            api = TradeAPI(client)
            # Tokens like "@current" become concrete ids before any job is
            # built, so every job below is pinned to a real league.
            self.cfg.leagues = await resolve_leagues(self.cfg, api, self.store)
            collector = Collector(self.cfg, api, self.store)
            try:
                jobs = build_jobs(self.cfg, collector)
            except Exception:
                # A malformed config must not turn into a launchd crash-loop
                # that silently stops all collection. Log and idle; the next
                # config edit (hot-reload) gets a chance to fix it.
                log.exception("could not build jobs from config; idling")
                jobs = []
            fingerprint = watchlist_fingerprint(self.cfg)
            if job_filter:
                jobs = [j for j in jobs if job_filter in j.name]
                if not jobs:
                    log.error("no jobs match %r", job_filter)
                    return
            log.info(
                "collector started: %d jobs, leagues=%s",
                len(jobs), ", ".join(self.cfg.leagues),
            )

            while not self._stop.is_set():
                now = utcnow()

                if not job_filter:
                    current = watchlist_fingerprint(self.cfg)
                    if current != fingerprint:
                        fingerprint = current
                        # Preserve timers for jobs that already existed, so a
                        # reload does not restart every schedule from zero and
                        # stampede the API.
                        previous = {j.name: j for j in jobs}
                        try:
                            new_cfg = load_config()
                            new_cfg.leagues = await resolve_leagues(
                                new_cfg, api, self.store
                            )
                            new_jobs = build_jobs(new_cfg, collector)
                        except Exception:
                            # Keep the running jobs on a bad edit rather than
                            # dropping collection.
                            log.exception("config reload failed; keeping previous jobs")
                            fingerprint = current
                            continue
                        self.cfg = new_cfg
                        collector.cfg = new_cfg
                        jobs = new_jobs
                        for job in jobs:
                            old = previous.get(job.name)
                            if old is not None:
                                job.next_run = old.next_run
                                job.failures = old.failures
                        log.info("watchlists changed; now %d jobs", len(jobs))

                due = [j for j in jobs if j.due(now)]

                for job in due:
                    if self._stop.is_set():
                        break
                    # A long gap means we were asleep, offline, or stopped;
                    # the job is due immediately and this records the catch-up.
                    if job.next_run and (now - job.next_run) > job.interval * CATCHUP_FACTOR:
                        log.info(
                            "%s: catching up, %s late",
                            job.name, str(now - job.next_run).split(".")[0],
                        )

                    try:
                        job.last_result = await job.run()
                        # Not every failure raises. A job that reports an error
                        # or collected nothing (a transient upstream hiccup,
                        # e.g. poe.ninja returning no usable rate) must also
                        # retry soon rather than sleep the full cadence.
                        if _is_soft_failure(job.last_result):
                            job.failures += 1
                            log.warning(
                                "%s produced no data (%d in a row) -> %s",
                                job.name, job.failures, job.last_result,
                            )
                        else:
                            if job.failures:
                                log.info("%s recovered after %d failure(s)",
                                         job.name, job.failures)
                            job.failures = 0
                            log.info("%s -> %s", job.name, job.last_result)
                    except Exception:
                        job.failures += 1
                        log.exception("%s failed (%d in a row)", job.name, job.failures)
                    finally:
                        job.reschedule(utcnow())

                if once:
                    return

                # Wake shortly before the next job is due; the loop is cheap
                # and the sleep keeps the process near-idle between passes.
                upcoming = [j.next_run for j in jobs if j.next_run]
                delay = 30.0
                if upcoming:
                    delay = max(
                        1.0, min(60.0, (min(upcoming) - utcnow()).total_seconds())
                    )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)

        log.info("collector stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="POE2 market collector daemon")
    parser.add_argument(
        "--once", action="store_true", help="Run every due job once, then exit."
    )
    parser.add_argument(
        "--job", default=None,
        help="Only run jobs whose name contains this substring.",
    )
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, (args.log_level or cfg.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    daemon = CollectorDaemon(cfg)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, daemon.request_stop)
        await daemon.run(once=args.once, job_filter=args.job)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
