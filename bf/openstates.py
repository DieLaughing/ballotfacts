"""Who represents an address in the state legislature now, from Open States."""
from __future__ import annotations

import httpx

from . import config
from .cache import Cache

URL = "https://v3.openstates.org/people.geo"
TTL = 24 * 3600
CHAMBERS = {"upper": "State Senate", "lower": "State House", "legislature": "Legislature"}


def parse(body: dict) -> list[dict]:
    out = []
    for p in body.get("results", []):
        role = p.get("current_role") or {}
        if (p.get("jurisdiction") or {}).get("classification") != "state" or role.get("org_classification") not in CHAMBERS:
            continue
        out.append({
            "name": p.get("name", ""),
            "party": p.get("party", ""),
            "chamber": CHAMBERS[role["org_classification"]],
            "district": role.get("district", ""),
            "url": p.get("openstates_url", ""),
        })
    out.sort(key=lambda r: r["chamber"] != "State Senate")
    return out


async def legislators(client: httpx.AsyncClient, cache: Cache, place_key: str, lat: float, lng: float) -> list[dict]:
    """Cached by district, never by address: everyone in the same districts gets the same answer."""
    if not config.OPENSTATES_KEY:
        return []

    async def load() -> list[dict]:
        r = await client.get(URL, params={"lat": lat, "lng": lng}, headers={"X-API-KEY": config.OPENSTATES_KEY})
        r.raise_for_status()
        return parse(r.json())

    return await cache.fetch(f"openstates:{place_key}", TTL, load)


async def by_district(client: httpx.AsyncClient, cache: Cache, state: str, upper: str, lower: str) -> list[dict]:
    """Same answer without coordinates: look each seat up by its district name."""
    if not config.OPENSTATES_KEY:
        return []

    async def load() -> list[dict]:
        out = []
        for chamber, name in (("upper", upper), ("lower", lower)):
            if not name:
                continue
            r = await client.get("https://v3.openstates.org/people", headers={"X-API-KEY": config.OPENSTATES_KEY}, params={
                "jurisdiction": state.lower(), "org_classification": chamber, "district": name.removesuffix(" District")})
            r.raise_for_status()
            out += parse({"results": [dict(p, jurisdiction={"classification": "state"}) for p in r.json().get("results", [])]})
        return out

    return await cache.fetch(f"openstates:{state}:{upper}:{lower}", TTL, load)
