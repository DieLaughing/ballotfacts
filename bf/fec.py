"""Federal races and money from the FEC's OpenFEC API.

Everything returned is plain JSON-ready dicts so it can sit in the cache.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from . import config
from .cache import Cache

log = logging.getLogger("bf.fec")
API = "https://api.open.fec.gov/v1/"
TTL = 3 * 3600  # FEC data changes about daily; outside-spending reports land within 24-48 hours

# Employer values that describe the donor, not who they work for.
NOT_EMPLOYERS = {
    "NOT EMPLOYED", "NO EMPLOYER", "NONE", "N/A", "NA", "RETIRED", "SELF", "SELF EMPLOYED", "SELF-EMPLOYED",
    "HOMEMAKER", "STUDENT", "UNEMPLOYED", "INFORMATION REQUESTED", "INFORMATION REQUESTED PER BEST EFFORTS",
    "REQUESTED", "", "DISABLED", "NULL",
}
HONORIFICS = {"MR", "MRS", "MS", "DR", "HON", "REV", "SEN", "SENATOR", "REP", "GOV", "JUDGE"}
SUFFIXES = {"JR", "SR", "II", "III", "IV"}


def display_name(fec_name: str) -> str:
    """'COLLINS, MICHAEL A JR' → 'Michael A. Collins Jr.' (the FEC stores LAST, FIRST MIDDLE)."""
    last, _, rest = fec_name.partition(",")
    words = [w.strip(".") for w in rest.split()]
    words = [w for w in words if w.upper() not in HONORIFICS]
    suffix = [w for w in words if w.upper() in SUFFIXES]
    given = [w for w in words if w.upper() not in SUFFIXES]

    def nice(w: str) -> str:
        w = w.title()
        w = re.sub(r"\bMc(\w)", lambda m: "Mc" + m.group(1).upper(), w)
        return w + "." if len(w) == 1 else w

    parts = [nice(w) for w in given] + [nice(last.strip())] + [s.title() + "." if s.upper() in {"JR", "SR"} else s.upper() for s in suffix]
    return " ".join(p for p in parts if p)


def party(full: str | None) -> str:
    p = (full or "").upper().removesuffix(" PARTY").strip()
    return p.title() if p else "No party listed"


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict:
    """The FEC API has the odd hiccup (5xx, or 429 when busy); two retries ride most of them out."""
    for attempt in range(3):
        r = await client.get(API + path, params={"api_key": config.FEC_KEY, **params})
        if r.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
            break
        log.warning("FEC %s on %s, retrying", r.status_code, path)
        await asyncio.sleep(1 + attempt * 2)
    r.raise_for_status()
    return r.json()


async def race(client: httpx.AsyncClient, cache: Cache, office: str, state: str, district: str = "") -> list[dict]:
    """Everyone who filed for one seat, the campaigns still in the general election first."""
    key = f"fec:race:{config.CYCLE}:{office}:{state}:{district}"

    async def load() -> list[dict]:
        q = {"cycle": config.CYCLE, "office": office, "state": state, "election_full": "true", "per_page": 100,
             "sort": "-total_receipts"}
        if office == "house":
            q["district"] = district
        rows = (await _get(client, "elections/", **q))["results"]
        try:
            spend = await _outside_by_candidate(client, office, state, district)
        except httpx.HTTPError as e:  # the race is still worth showing without it
            log.warning("outside spending unavailable: %s", type(e).__name__)  # str(e) would include the API key
            spend = {}
        out = []
        for r in rows:
            out.append({
                "id": r["candidate_id"],
                "name": display_name(r["candidate_name"]),
                "party": party(r.get("party_full")),
                "incumbent": r.get("incumbent_challenge_full") == "Incumbent",
                "raised": r.get("total_receipts") or 0,
                "cash": r.get("cash_on_hand_end_period") or 0,
                "through": r.get("coverage_end_date") or "",
                "running": (r.get("coverage_end_date") or "") >= config.PRE_GENERAL_THROUGH,
                "support": spend.get(r["candidate_id"], {}).get("S", 0),
                "oppose": spend.get(r["candidate_id"], {}).get("O", 0),
            })
        out.sort(key=lambda c: (not c["running"], -c["raised"]))
        return out

    return await cache.fetch(key, TTL, load)


async def _outside_by_candidate(client: httpx.AsyncClient, office: str, state: str, district: str) -> dict:
    q = {"cycle": config.CYCLE, "office": office, "state": state, "per_page": 100}
    if office == "house":
        q["district"] = district
    totals: dict[str, dict[str, float]] = {}
    for r in (await _get(client, "schedules/schedule_e/by_candidate/", **q))["results"]:
        side = totals.setdefault(r["candidate_id"], {"S": 0, "O": 0})
        side[r["support_oppose_indicator"]] = side.get(r["support_oppose_indicator"], 0) + (r["total"] or 0)
    return totals


async def candidate(client: httpx.AsyncClient, cache: Cache, cid: str) -> dict | None:
    key = f"fec:cand:{config.CYCLE}:{cid}"

    async def load() -> dict | None:
        found = (await _get(client, f"candidate/{cid}/"))["results"]
        if not found:
            return None
        c = found[0]
        totals = (await _get(client, f"candidate/{cid}/totals/", cycle=config.CYCLE, election_full="true"))["results"]
        t = totals[0] if totals else {}
        outside = (await _get(client, "schedules/schedule_e/by_candidate/", candidate_id=cid, cycle=config.CYCLE,
                              per_page=100, sort="-total"))["results"]
        committees = (await _get(client, f"candidate/{cid}/committees/", designation="P", cycle=config.CYCLE))["results"]
        pcc = committees[0]["committee_id"] if committees else None
        employers = []
        if pcc:
            rows = (await _get(client, "schedules/schedule_a/by_employer/", committee_id=pcc, cycle=config.CYCLE,
                               sort="-total", per_page=40))["results"]
            employers = [{"name": r["employer"].title(), "total": r["total"]}
                         for r in rows if (r.get("employer") or "").upper().strip(" .") not in NOT_EMPLOYERS][:10]
        return {
            "id": cid,
            "name": display_name(c["name"]),
            "party": party(c.get("party_full")),
            "office": c.get("office"),
            "state": c.get("state"),
            "district": c.get("district") or "",
            "incumbent": c.get("incumbent_challenge") == "I",
            "committee": pcc,
            "committee_name": committees[0].get("name", "") if committees else "",
            "raised": t.get("receipts") or 0,
            "spent": t.get("disbursements") or 0,
            "cash": t.get("last_cash_on_hand_end_period") or 0,
            "through": (t.get("coverage_end_date") or "")[:10],
            "sources": [
                ("Small donors (under $200)", t.get("individual_unitemized_contributions") or 0),
                ("Larger donors ($200+)", t.get("individual_itemized_contributions") or 0),
                ("PACs and other committees", t.get("other_political_committee_contributions") or 0),
                ("Party committees", t.get("political_party_committee_contributions") or 0),
                ("The candidate", (t.get("candidate_contribution") or 0) + (t.get("loans_made_by_candidate") or 0)),
                ("Transfers from joint fundraisers", t.get("transfers_from_other_authorized_committee") or 0),
            ],
            # No committee id = a group or person reporting spending without a registered committee
            "outside": [{"name": r["committee_name"] or "Unregistered group or person", "id": r["committee_id"], "side": r["support_oppose_indicator"],
                         "total": r["total"] or 0} for r in outside if (r["total"] or 0) > 0],
            "employers": employers,
        }

    return await cache.fetch(key, TTL, load)
