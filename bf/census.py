"""Address → districts, with the US Census geocoder (free, no key).

The address is sent to the Census Bureau once and then forgotten: nothing here stores or logs it.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .states import STATES

URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"


@dataclass
class Place:
    matched: str          # the address as the Census Bureau understood it
    state: str            # postal code, e.g. "PA"
    state_name: str
    district: str         # congressional district, "00" for at-large seats
    county: str
    town: str             # county subdivision, e.g. "Falmouth town" (New England towns, townships elsewhere)
    state_upper: str      # state senate district name, "" where there is none (e.g. Nebraska has one chamber)
    state_lower: str
    lat: float
    lng: float

    @property
    def district_label(self) -> str:
        return "At-large" if self.district == "00" else f"District {int(self.district)}"

    @property
    def key(self) -> str:
        """A link to this ballot that names districts and the town, never the street address.
        Everyone in the same districts shares it, so it can be cached and passed around."""
        q = {"town": self.town, "county": self.county, "up": self.state_upper, "lo": self.state_lower}
        return f"/ballot/{self.state}/{self.district}?" + urlencode({k: v for k, v in q.items() if v})

    @classmethod
    def from_key(cls, state: str, district: str, q: dict) -> "Place":
        from .states import BY_CODE
        town = q.get("town", "")
        label = ", ".join(x for x in (town.removesuffix(" town").removesuffix(" city"), state) if x)
        return cls(matched=label, state=state, state_name=BY_CODE[state], district=district, county=q.get("county", ""),
                   town=town, state_upper=q.get("up", ""), state_lower=q.get("lo", ""), lat=0.0, lng=0.0)


def _first(geos: dict, needle: str) -> dict:
    for name, rows in geos.items():
        if needle in name and rows:
            return rows[0]
    return {}


def _tidy(matched: str) -> str:
    """'1500 MARKET ST, PHILADELPHIA, PA, 19102' → '1500 Market St, Philadelphia, PA 19102'."""
    parts = [p.strip() for p in matched.split(",")]
    if len(parts) >= 3:
        return ", ".join(p.title() for p in parts[:-2]) + f", {parts[-2].upper()} {parts[-1]}"
    return matched.title()


def parse(body: dict) -> Place | None:
    matches = body.get("result", {}).get("addressMatches") or []
    if not matches:
        return None
    m = matches[0]
    geos = m["geographies"]
    state = STATES.get(_first(geos, "States").get("STATE", ""))
    cd = _first(geos, "Congressional Districts")
    if not state or not cd:
        return None
    return Place(
        matched=_tidy(m["matchedAddress"]),
        state=state[0],
        state_name=state[1],
        district=cd.get("GEOID", "")[-2:] or "00",
        county=_first(geos, "Counties").get("NAME", ""),
        town=_first(geos, "County Subdivisions").get("NAME", ""),
        state_upper=_first(geos, "State Legislative Districts - Upper").get("NAME", ""),
        state_lower=_first(geos, "State Legislative Districts - Lower").get("NAME", ""),
        lat=float(m["coordinates"]["y"]),
        lng=float(m["coordinates"]["x"]),
    )


async def lookup(client: httpx.AsyncClient, address: str) -> Place | None:
    r = await client.get(URL, params={
        "address": address, "benchmark": "Public_AR_Current", "vintage": "Current_Current",
        "layers": "all", "format": "json",
    })
    r.raise_for_status()
    return parse(r.json())
