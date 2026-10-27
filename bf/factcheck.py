"""Published fact checks from Google's Fact Check Tools API (ClaimReview data from PolitiFact,
FactCheck.org, AP, Reuters, newsrooms and others).

Ratings are shown in each fact-checker's own words; Ballot Facts never rates a claim itself.
"""
from __future__ import annotations

import re

import httpx

from . import config
from .cache import Cache

URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
TTL = 6 * 3600


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]", "", s.lower())


def about(claim: dict, firsts: list[str], last: str) -> bool:
    """Name searches are fuzzy ('Brooks' finds Garth Brooks), so keep a claim only when the
    candidate's last name and one of their first names appear in who said it, what was said,
    or the review title."""
    review = (claim.get("claimReview") or [{}])[0]
    hay = _norm(" ".join([claim.get("claimant") or "", claim.get("text") or "", review.get("title") or ""]))
    if not re.search(rf"\b{_norm(last)}\b", hay):
        return False
    return any(re.search(rf"\b{_norm(f)}\b", hay) for f in firsts if f)


def parse(body: dict, firsts: list[str], last: str) -> list[dict]:
    out = []
    for c in body.get("claims", []):
        if not about(c, firsts, last):
            continue
        for r in c.get("claimReview", [])[:1]:
            out.append({
                "claim": c.get("text", "").strip(),
                "claimant": c.get("claimant") or "",
                "said": (c.get("claimDate") or "")[:10],
                "by": (r.get("publisher") or {}).get("name") or (r.get("publisher") or {}).get("site") or "Fact-checker",
                "rating": r.get("textualRating") or "",
                "url": r.get("url") or "",
                "date": (r.get("reviewDate") or "")[:10],
            })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


async def search(client: httpx.AsyncClient, cache: Cache, firsts: list[str], last: str) -> list[dict]:
    """firsts: the names the candidate might go by, e.g. their legal first name and the one on
    their campaign committee ('Jonathan' and 'Jon')."""
    if not config.GOOGLE_KEY:
        return []
    key = f"factcheck:{'/'.join(f.lower() for f in firsts)}:{last.lower()}"

    async def load() -> list[dict]:
        r = await client.get(URL, params={"key": config.GOOGLE_KEY, "query": f"{firsts[0]} {last}", "languageCode": "en-US",
                                          "pageSize": 50, "maxAgeDays": 1100})
        r.raise_for_status()
        return parse(r.json(), firsts, last)

    return await cache.fetch(key, TTL, load)
