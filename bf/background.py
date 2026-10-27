"""Jobs that keep the site's data fresh without anyone waiting on them.

- Every 3 hours: load every House and Senate race (and its outside spending) into the cache, so a
  voter's first lookup doesn't wait on a slow FEC response.
- Every day: re-add up Massachusetts independent expenditures from OCPF's reports.

Both run inside the web app, a few requests a second at most, and log one line when they finish.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import fec, ocpf
from .cache import Cache
from .states import BY_CODE

log = logging.getLogger("bf.background")
WARM_EVERY = 3 * 3600
OUTSIDE_EVERY_HOURS = 20


async def warm_races(client: httpx.AsyncClient, cache: Cache) -> None:
    started, failed = time.monotonic(), 0
    races = [s for s in await fec.seats(client) if s[1] in BY_CODE]
    for office, state, district in races:
        try:
            await fec.race(client, cache, office, state, district)
        except httpx.HTTPError:
            failed += 1
        await asyncio.sleep(0.5)  # about 2 requests a second, well under the FEC's 1,000 an hour
    log.warning("warmed %d federal races in %.0f s (%d failed)", len(races), time.monotonic() - started, failed)


async def run(client: httpx.AsyncClient, cache: Cache) -> None:
    await asyncio.sleep(30)  # let the app finish starting
    last_warm = 0.0
    while True:
        try:
            if ocpf.outside_age_hours() > OUTSIDE_EVERY_HOURS:
                await ocpf.refresh_outside(client)
                log.warning("refreshed Massachusetts independent expenditures")
            if time.monotonic() - last_warm > WARM_EVERY:
                await warm_races(client, cache)
                last_warm = time.monotonic()
        except Exception as e:  # never let a bad day at a data source stop the loop
            log.warning("background job failed: %s", type(e).__name__)
        await asyncio.sleep(600)
