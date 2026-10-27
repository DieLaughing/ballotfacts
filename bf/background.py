"""Jobs that keep the site's data fresh without anyone waiting on them.

- Continuously, one race every 20 seconds (about 3 hours a round): load every House and Senate race
  and its outside spending into the cache, so a voter's first lookup doesn't wait on a slow FEC
  response. That's ~350 FEC requests an hour, leaving most of the key's 1,000/hour for visitors.
  Set BF_WARM=0 to switch it off (staging does, so it doesn't spend the shared key's allowance).
- Every day: re-add up Massachusetts independent expenditures from OCPF's reports, and fetch
  any candidate photos we don't have yet (bf/photos.py).

Both run inside the web app, a few requests a second at most, and log one line when they finish.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import config, fec, ocpf, photos
from .cache import Cache
from .states import BY_CODE

log = logging.getLogger("bf.background")
WARM_PAUSE = 20  # seconds between races
OUTSIDE_EVERY_HOURS = 20


async def warm_races(client: httpx.AsyncClient, cache: Cache) -> None:
    started, failed = time.monotonic(), 0
    races = [s for s in await fec.seats(client) if s[1] in BY_CODE]
    for office, state, district in races:
        try:
            await fec.race(client, cache, office, state, district)
        except httpx.HTTPError:
            failed += 1
        await asyncio.sleep(WARM_PAUSE)
    log.warning("warmed %d federal races in %.0f s (%d failed)", len(races), time.monotonic() - started, failed)


async def run(client: httpx.AsyncClient, cache: Cache) -> None:
    await asyncio.sleep(30)  # let the app finish starting
    while True:
        try:
            if ocpf.outside_age_hours() > OUTSIDE_EVERY_HOURS:
                await ocpf.refresh_outside(client)
                log.warning("refreshed Massachusetts independent expenditures")
            if photos.age_hours() > OUTSIDE_EVERY_HOURS:
                await photos.refresh(client, cache, await ocpf.candidates(client, cache))
            if config.WARM:
                await warm_races(client, cache)  # takes ~3 hours, then the loop comes round again
        except Exception as e:  # never let a bad day at a data source stop the loop
            log.warning("background job failed: %s", type(e).__name__)
        await asyncio.sleep(600)
