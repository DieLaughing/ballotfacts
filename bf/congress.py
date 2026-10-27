"""What sitting members of Congress have done in office: how they voted and which bills they wrote.

- House votes: every roll call of the current Congress, from the House Clerk's official vote files
  (clerk.house.gov/evs, one public XML file per vote, no key). They're downloaded once by the
  background job and kept in the data folder; new votes are added each round.
- Bills sponsored: from Congress.gov's API (the Library of Congress), with the same api.data.gov
  key the FEC uses.
- Senate votes aren't shown yet: senate.gov refuses automated downloads, and we don't get around that.

Nothing here is picked by hand. Every vote is listed, newest first, with its official description.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from . import config
from .cache import Cache

log = logging.getLogger("bf.congress")
CONGRESS = 119  # 2025–2026
YEARS = (2025, 2026)  # session 1 and 2
CLERK = "https://clerk.house.gov/evs/{year}/roll{roll:03d}.xml"
API = "https://api.congress.gov/v3"
FILE = config.DATA / f"house-votes-{CONGRESS}.json"
PAUSE = 1  # seconds between Clerk downloads
# One letter per vote, so a member's whole record is one short string.
CAST = {"Yea": "Y", "Aye": "Y", "Nay": "N", "No": "N", "Present": "P", "Not Voting": "-"}
SAY = {"Y": "Yes", "N": "No", "P": "Present", "-": "Didn't vote"}
# Votes that decide a bill or resolution itself, going by the vote's official question. Everything
# else (rules for debate, amendments, motions, quorum calls) is listed separately as other votes.
FINAL = re.compile(r"Passage|Suspend the Rules and (Pass|Agree)|Concur|Conference Report|Overrid", re.IGNORECASE)
BILL_TYPES = {"H R": "hr", "H RES": "hres", "H J RES": "hjres", "H CON RES": "hconres",
              "S": "s", "S RES": "sres", "S J RES": "sjres", "S CON RES": "sconres"}
SHORT = {"hr": "H.R.", "hres": "H.Res.", "hjres": "H.J.Res.", "hconres": "H.Con.Res.",
         "s": "S.", "sres": "S.Res.", "sjres": "S.J.Res.", "sconres": "S.Con.Res."}
BILL_PATH = {"hr": "house-bill", "hres": "house-resolution", "hjres": "house-joint-resolution",
             "hconres": "house-concurrent-resolution", "s": "senate-bill", "sres": "senate-resolution",
             "sjres": "senate-joint-resolution", "sconres": "senate-concurrent-resolution"}


def _load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {"votes": [], "casts": {}}


def bill_name(legis: str) -> str:
    """'H R 4626' → 'H.R. 4626', the way Congress.gov writes it."""
    kind, _, num = legis.strip().rpartition(" ")
    t = BILL_TYPES.get(kind)
    return f"{SHORT[t]} {num}" if t else legis


def bill_link(legis: str) -> str:
    """'H R 4626' → its Congress.gov page."""
    kind, _, num = legis.strip().rpartition(" ")
    t = BILL_TYPES.get(kind)
    return f"https://www.congress.gov/bill/{CONGRESS}th-congress/{BILL_PATH[t]}/{num}" if t and num.isdigit() else ""


def parse_roll(xml: bytes) -> tuple[dict, dict[str, str]] | None:
    """One Clerk vote file → (the vote, {bioguide id: one-letter cast}). None if it isn't a vote:
    asking for a vote number that doesn't exist yet gets a short error file, not a 404."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    m = root.find("vote-metadata")
    if m is None:
        return None
    text = lambda tag: (m.findtext(tag) or "").strip()
    legis = text("legis-num")
    vote = {
        "roll": int(text("rollcall-num")),
        "date": datetime.strptime(text("action-date"), "%d-%b-%Y").date().isoformat(),
        "question": text("vote-question"),
        "desc": text("vote-desc"),
        "legis": legis if legis and legis != "QUORUM" else "",
        "result": text("vote-result"),
    }
    vote["final"] = bool(FINAL.search(vote["question"]))
    casts = {}
    for rv in root.iter("recorded-vote"):
        who = rv.find("legislator")
        if who is not None and who.get("name-id"):
            casts[who.get("name-id")] = CAST.get((rv.findtext("vote") or "").strip(), "-")
    return vote, casts


def _save(data: dict) -> None:
    n = len(data["votes"])
    # Members who left the House get '.' (not a member) for the votes after they left.
    data["casts"] = {k: v.ljust(n, ".") for k, v in data["casts"].items()}
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(FILE)


async def refresh_votes(client: httpx.AsyncClient) -> int:
    """Download the House votes we don't have yet, saving as it goes. Returns how many were added."""
    data = _load()
    added = 0
    for year in YEARS:
        roll = max([v["roll"] for v in data["votes"] if v["year"] == year], default=0) + 1
        while True:
            r = await client.get(CLERK.format(year=year, roll=roll))
            parsed = parse_roll(r.content) if r.status_code == 200 else None
            if r.status_code >= 500:
                r.raise_for_status()  # try again next round
            if not parsed:
                break  # no vote with this number yet
            vote, casts = parsed
            vote["year"] = year
            i = len(data["votes"])
            data["votes"].append(vote)
            for who, cast in casts.items():
                # Members who weren't in the House yet get '.' (not a member) for earlier votes.
                data["casts"][who] = data["casts"].get(who, "").ljust(i, ".") + cast
            added += 1
            roll += 1
            if added % 50 == 0:
                _save(data)
            await asyncio.sleep(PAUSE)
    if added:
        _save(data)
    return added


_cached: tuple[float, dict] | None = None


def _data() -> dict:
    """The vote file, re-read only when the background job has changed it."""
    global _cached
    try:
        mtime = FILE.stat().st_mtime
    except OSError:
        return {"votes": [], "casts": {}}
    if not _cached or _cached[0] != mtime:
        _cached = (mtime, _load())
    return _cached[1]


def house_record(bioguide: str) -> dict | None:
    """A member's House votes this Congress, newest first, or None if they cast none."""
    data = _data()
    casts = data["casts"].get(bioguide)
    if not casts:
        return None
    rows = [dict(v, cast=SAY[c], link=bill_link(v["legis"]), legis=bill_name(v["legis"])) for v, c in zip(data["votes"], casts) if c != "."]
    if not rows:
        return None
    rows.sort(key=lambda v: (v["date"], v["roll"]), reverse=True)
    missed = sum(v["cast"] == SAY["-"] for v in rows)
    return {
        "final": [v for v in rows if v["final"]],
        "other": [v for v in rows if not v["final"]],
        "total": len(rows),
        "missed": missed,
        "missed_pct": round(100 * missed / len(rows), 1),
        "through": rows[0]["date"],
    }


async def sponsored(client: httpx.AsyncClient, cache: Cache, bioguide: str) -> dict:
    """Bills and resolutions the member introduced this Congress, newest first."""
    async def load() -> dict:
        r = await client.get(f"{API}/member/{bioguide}/sponsored-legislation",
                             params={"api_key": config.FEC_KEY, "format": "json", "limit": 250})
        r.raise_for_status()
        bills = [b for b in r.json().get("sponsoredLegislation", []) if b.get("congress") == CONGRESS and b.get("number")]
        rows = [{
            "title": b.get("title") or "",
            "number": f"{SHORT[b['type'].lower()]} {b['number']}",
            "introduced": b.get("introducedDate") or "",
            "latest": (b.get("latestAction") or {}).get("text") or "",
            "law": "Became Public Law" in ((b.get("latestAction") or {}).get("text") or ""),
            "link": f"https://www.congress.gov/bill/{CONGRESS}th-congress/{BILL_PATH.get(b['type'].lower(), '')}/{b['number']}",
        } for b in bills if b.get("type", "").lower() in BILL_PATH]
        rows.sort(key=lambda b: b["introduced"], reverse=True)
        return {"bills": rows, "laws": sum(b["law"] for b in rows)}

    return await cache.fetch(f"congress:sponsored:v2:{CONGRESS}:{bioguide}", 24 * 3600, load)

