"""Candidate photos, downloaded once, shrunk to small thumbnails and served from this site (the page
never loads anything from another site).

- Members of Congress: official portraits from the unitedstates project (public domain), matched
  to FEC candidate ids through its legislators-current list.
- Massachusetts officeholders (legislators, governor and so on): their official portrait, linked
  from Open States; other Massachusetts candidates: the photo the campaign filed with OCPF, if any.
Candidates without a photo keep their initials.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time

import httpx
from PIL import Image, ImageOps

from . import config
from .cache import Cache

log = logging.getLogger("bf.photos")
DIR = config.DATA / "photos"
LEGISLATORS = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
CONGRESS_PHOTO = "https://unitedstates.github.io/images/congress/225x275/{}.jpg"
OCPF_PHOTO = "https://ocpf2.blob.core.windows.net/filers-photos/{}.png"
SIZE = 192
RETRY_MISSING_AFTER = 7 * 24 * 3600
NAME = re.compile(r"(fed-[HSP]\d[A-Z0-9]{7}|ma-\d{1,7})\.jpg")


def url(kind: str, ident: str | int) -> str:
    """'/photo/fed-S8GA00180.jpg' when we have it, else '' (show initials)."""
    name = f"{kind}-{ident}.jpg"
    return f"/photo/{name}" if (DIR / name).exists() else ""


async def congress_ids(client: httpx.AsyncClient, cache: Cache) -> dict[str, str]:
    """FEC candidate id → Bioguide id, for sitting members of Congress."""
    async def load() -> dict[str, str]:
        r = await client.get(LEGISLATORS)
        r.raise_for_status()
        return {fec: m["id"]["bioguide"] for m in r.json() for fec in m["id"].get("fec", [])}

    return await cache.fetch("photos:congress-ids", 24 * 3600, load)


async def fetch(client: httpx.AsyncClient, kind: str, ident: str | int, source: str, try_anyway: bool = False) -> bool:
    """Download, crop to a square and save one thumbnail. Remembers misses for a week, unless
    try_anyway (a second source for someone the first one had no photo of)."""
    DIR.mkdir(parents=True, exist_ok=True)
    out, miss = DIR / f"{kind}-{ident}.jpg", DIR / f"{kind}-{ident}.none"
    recent_miss = miss.exists() and time.time() - miss.stat().st_mtime < RETRY_MISSING_AFTER
    if out.exists() or (recent_miss and not try_anyway):
        return out.exists()
    r = await client.get(source)
    if r.status_code != 200 or len(r.content) > 8_000_000:
        miss.touch()
        return False
    try:
        img = ImageOps.fit(ImageOps.exif_transpose(Image.open(io.BytesIO(r.content))).convert("RGB"),
                           (SIZE, SIZE), centering=(0.5, 0.3))  # faces sit in the upper part of portraits
    except Exception:
        miss.touch()
        return False
    tmp = out.with_suffix(".tmp")
    img.save(tmp, "JPEG", quality=82, optimize=True)
    tmp.replace(out)
    miss.unlink(missing_ok=True)
    return True


async def official_ma(client: httpx.AsyncClient) -> list[dict]:
    """Massachusetts officeholders with an official portrait, from Open States."""
    if not config.OPENSTATES_KEY:
        return []
    people, page = [], 1
    while True:
        r = await client.get("https://v3.openstates.org/people", headers={"X-API-KEY": config.OPENSTATES_KEY},
                             params={"jurisdiction": "ma", "per_page": 50, "page": page})
        r.raise_for_status()
        d = r.json()
        people += [{"name": p["name"], "image": p["image"]} for p in d["results"] if p.get("image")]
        if page >= d["pagination"]["max_page"]:
            return people
        page += 1
        await asyncio.sleep(7)  # Open States' free tier allows about 10 requests a minute


def _same_person(official: str, ocpf_name: str) -> bool:
    """'Dave Vieira' and 'Vieira, David T.': same last name, same first initial."""
    last, _, first = ocpf_name.partition(",")
    words = official.replace(".", "").split()
    return bool(words) and words[-1].lower() == last.strip().lower() and words[0][:1].lower() == first.strip()[:1].lower()


async def refresh(client: httpx.AsyncClient, cache: Cache, ma_candidates: list[dict]) -> None:
    """Fill in any photos we don't have yet; existing ones are kept."""
    got = 0
    for fec_id, bioguide in (await congress_ids(client, cache)).items():
        got += await fetch(client, "fed", fec_id, CONGRESS_PHOTO.format(bioguide))
        await asyncio.sleep(0.2)
    officials = await official_ma(client)
    for c in ma_candidates:
        match = [o for o in officials if _same_person(o["name"], c["name"])]
        if len(match) == 1:  # an official portrait beats a campaign photo
            got += await fetch(client, "ma", c["cpf"], match[0]["image"], try_anyway=True)
        else:
            got += await fetch(client, "ma", c["cpf"], OCPF_PHOTO.format(c["cpf"]))
        await asyncio.sleep(0.2)
    (DIR / ".refreshed").touch()
    log.warning("photos ready: %d", got)


def age_hours() -> float:
    try:
        return (time.time() - (DIR / ".refreshed").stat().st_mtime) / 3600
    except OSError:
        return 1e9
