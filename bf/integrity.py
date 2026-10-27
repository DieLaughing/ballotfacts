"""Links to a candidate's profile on Integrity Index (integrityindex.us), an outside scorecard.

Integrity Index is run by the Political Integrity PAC, a Super PAC, and grades politicians with its
own formula. Ballot Facts only links to it, clearly labelled; it doesn't copy or show the grade.
Their robots.txt allows profile pages and blocks their API, so this reads their public sitemap
(once a day) and a profile page (once a week) to confirm it's the same person before linking.
"""
from __future__ import annotations

import re

import httpx

from .cache import Cache

SITE = "https://integrityindex.us"


def slugify(*words: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", " ".join(words).lower()).strip("-")


async def profile(client: httpx.AsyncClient, cache: Cache, firsts: list[str], last: str, state_name: str) -> str:
    """Their profile URL for this candidate, or "" when there isn't one we're sure about."""
    async def sitemap() -> list[str]:
        r = await client.get(f"{SITE}/sitemap.xml")
        r.raise_for_status()
        return re.findall(r"<loc>" + re.escape(SITE) + r"/candidate/([a-z0-9-]+)</loc>", r.text)

    known = set(await cache.fetch("integrity:sitemap", 24 * 3600, sitemap))
    for first in firsts:
        slug = slugify(first, last)
        if slug not in known:
            continue

        async def check() -> bool:
            r = await client.get(f"{SITE}/candidate/{slug}")
            # Same name isn't enough (there's more than one Chris Smith): the page must name the state too.
            return r.status_code == 200 and state_name in r.text and last in r.text

        if await cache.fetch(f"integrity:check:{slug}:{state_name}", 7 * 24 * 3600, check):
            return f"{SITE}/candidate/{slug}"
    return ""
