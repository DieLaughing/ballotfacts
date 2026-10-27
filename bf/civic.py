"""Where and how to vote, from Google's Civic Information API (Voting Information Project data).

Google no longer returns ballot contests here, only voting locations and official links, so
that's all this module asks for. Results depend on the voter's address and are never cached.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from . import config

URL = "https://www.googleapis.com/civicinfo/v2/voterinfo"

SAMPLE_BALLOT = "See your official sample ballot"
# Google's field → what we call the link, in the order voters usually need them.
LINKS = [
    ("electionRegistrationConfirmationUrl", "Check your registration"),
    ("ballotInfoUrl", SAMPLE_BALLOT),
    ("votingLocationFinderUrl", "Official polling place finder"),
    ("absenteeVotingInfoUrl", "Mail and absentee voting"),
    ("electionRegistrationUrl", "Register to vote"),
    ("electionInfoUrl", "Election information"),
    ("electionRulesUrl", "Voting rules and ID"),
]


@dataclass
class Site:
    name: str
    address: str
    hours: str
    dates: str
    miles: float | None


@dataclass
class Office:
    name: str
    links: list[tuple[str, str]]


@dataclass
class VoterInfo:
    polling: list[Site] = field(default_factory=list)
    early: list[Site] = field(default_factory=list)
    dropoff: list[Site] = field(default_factory=list)
    offices: list[Office] = field(default_factory=list)

    @property
    def sample_ballot(self) -> str | None:
        for office in reversed(self.offices):  # the county's own link beats the state's
            for url, label in office.links:
                if label == SAMPLE_BALLOT:
                    return url
        return None


def _miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2
    return 7918 * math.asin(math.sqrt(a))


def _hours(text: str, today: date) -> str:
    """Google sends early-voting hours as one line per day for the whole period. Keep the days
    still to come, and only the next few of those."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) <= 3:
        return "\n".join(lines)
    upcoming = []
    for line in lines:
        m = re.match(r"\w{3}, (\w{3} \d{1,2}):", line)
        try:
            day = datetime.strptime(f"{m.group(1)} {today.year}", "%b %d %Y").date() if m else None
        except ValueError:
            day = None
        if day is None or day >= today:
            upcoming.append(line)
    more = len(upcoming) - 3
    return "\n".join(upcoming[:3]) + (f"\nand {more} more day{'s' if more > 1 else ''}" if more > 0 else "")


def _sites(rows: list[dict], lat: float, lng: float, limit: int) -> list[Site]:
    out = []
    for r in rows:
        a = r.get("address", {})
        street = ", ".join(x.title() for x in (a.get("line1"), a.get("city")) if x)
        street = ", ".join(x for x in (street, (a.get("state") or "").upper()) if x)
        dates = " to ".join(x for x in (r.get("startDate"), r.get("endDate")) if x)
        miles = _miles(lat, lng, r["latitude"], r["longitude"]) if "latitude" in r else None
        out.append(Site(a.get("locationName", "").title() or street, street, _hours(r.get("pollingHours", ""), date.today()),
                        dates, miles))
    out.sort(key=lambda s: s.miles if s.miles is not None else 1e9)
    return out[:limit]


def _office(body: dict, fallback: str) -> Office | None:
    eab = body.get("electionAdministrationBody") or {}
    links = [(eab[k], label) for k, label in LINKS if eab.get(k)]
    return Office(eab.get("name") or fallback, links) if links else None


def parse(body: dict, lat: float, lng: float) -> VoterInfo:
    info = VoterInfo(
        polling=_sites(body.get("pollingLocations", []), lat, lng, 3),
        early=_sites(body.get("earlyVoteSites", []), lat, lng, 3),
        dropoff=_sites(body.get("dropOffLocations", []), lat, lng, 3),
    )
    for st in body.get("state", []):
        if office := _office(st, st.get("name", "State election office")):
            info.offices.append(office)
        local = st.get("local_jurisdiction") or {}
        if office := _office(local, (local.get("name") or "County").title() + " election office"):
            info.offices.append(office)
    return info


async def lookup(client: httpx.AsyncClient, address: str, lat: float, lng: float) -> VoterInfo | None:
    if not config.GOOGLE_KEY:
        return None
    r = await client.get(URL, params={"key": config.GOOGLE_KEY, "address": address, "electionId": config.GOOGLE_ELECTION_ID})
    if r.status_code == 400:  # "Election unknown": Google has nothing for this address
        return None
    r.raise_for_status()
    return parse(r.json(), lat, lng)
