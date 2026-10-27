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
# FEC committee types in plain words. Factual definitions only; no labels about who they back.
KINDS = {
    "O": "Super PAC. It can raise unlimited money from people, companies and unions and spend it for or against candidates, but it can't give money to campaigns or coordinate with them.",
    "U": "Super PAC run by a single candidate's supporters (single-candidate Super PAC). Same rules: unlimited money, no coordination with the campaign.",
    "V": "Hybrid PAC. It has a Super PAC account (unlimited money, spent independently) and a separate account with contribution limits that can give to candidates.",
    "W": "Hybrid PAC. It has a Super PAC account (unlimited money, spent independently) and a separate account with contribution limits that can give to candidates.",
    "X": "Political party committee.", "Y": "Political party committee.", "Z": "National party committee account.",
    "N": "PAC. It collects contributions within federal limits and can give to candidates.",
    "Q": "PAC. It collects contributions within federal limits and can give to candidates.",
    "I": "A person or group that isn't a registered PAC but reported spending on its own. Groups like this only have to disclose donors who gave specifically for that spending.",
    "C": "An organization reporting the cost of messages to its own members (employees, union members or shareholders).",
    "E": "Electioneering communication filer: a group that reported paying for broadcast ads naming candidates close to an election.",
}
SPONSORS = {"C": "a corporation", "L": "a labor union", "M": "a membership organization", "T": "a trade association",
            "V": "a cooperative", "W": "a corporation without capital stock"}

# Payment platforms that pass along many people's small donations under their own name.
CONDUITS = {"WINRED", "ACTBLUE", "WINRED, INC.", "ACTBLUE TECHNICAL SERVICES"}
UPPER_WORDS = {"LLC", "INC", "PAC", "LP", "LLP", "USA", "US", "PC", "NA", "II", "III", "IV", "CEO"}


def tidy(name: str) -> str:
    """FEC names are all caps; 'A16Z CAPITAL MANAGEMENT LLC.' → 'A16Z Capital Management LLC.'"""
    return " ".join(w if w.strip(".,").upper() in UPPER_WORDS else w.title() for w in name.upper().split())


HONORIFICS = {"MR", "MRS", "MS", "DR", "HON", "REV", "SEN", "SENATOR", "REP", "GOV", "JUDGE"}
SUFFIXES = {"JR", "SR", "II", "III", "IV"}


def website(raw: str | None) -> str:
    """Campaigns type their own website on FEC Form 1: 'HTTPS://ELECTJON.COM/', 'nikemaforcongress.com'."""
    raw = (raw or "").strip().lower()
    if not raw or " " in raw or "." not in raw:
        return ""
    return raw if raw.startswith(("http://", "https://")) else "https://" + raw


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


def _about(c: dict) -> dict:
    """The facts shown when someone taps a group: what kind it is, in plain words, and who's behind it."""
    kind = KINDS.get(c.get("committee_type") or "", (c.get("committee_type_full") or "Committee") + ".")
    sponsor = SPONSORS.get(c.get("organization_type") or "")
    if sponsor and c.get("committee_type") in ("N", "Q"):
        kind = f"PAC connected to {sponsor}, funded by contributions from its employees, members or shareholders. It can give to candidates within federal limits."
    return {
        "kind": kind,
        "party": party(c["party_full"]) if c.get("party_full") else "",
        "since": (c.get("first_file_date") or "")[:4],
        "state": c.get("state") or "",
        "linked": (c.get("affiliated_committee_name") or "").title(),
        "leadership": c.get("leadership_pac") == "Y" or c.get("designation") == "D",
    }


async def _groups(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
    """Facts for many committees in as few requests as possible (the API takes up to 100 ids at once)."""
    out: dict[str, dict] = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 100):
        try:
            rows = (await _get(client, "committees/", committee_id=ids[i:i + 100], per_page=100))["results"]
        except httpx.HTTPError as e:
            log.warning("committee details unavailable: %s", type(e).__name__)
            continue
        out.update({c["committee_id"]: _about(c) for c in rows})
    return out


async def group(client: httpx.AsyncClient, cache: Cache, gid: str) -> dict | None:
    """A group's own page: what it is, who funds it this cycle, and which candidates it spends on."""
    key = f"fec:group:{config.CYCLE}:{gid}"

    async def load() -> dict | None:
        found = (await _get(client, f"committee/{gid}/"))["results"]
        if not found:
            return None
        c = found[0]
        totals = (await _get(client, f"committee/{gid}/totals/", cycle=config.CYCLE))["results"]
        t = totals[0] if totals else {}
        rows = (await _get(client, "schedules/schedule_a/", committee_id=gid, two_year_transaction_period=config.CYCLE,
                           sort="-contribution_receipt_amount", per_page=100))["results"]
        # The by-candidate summary needs a state for House races, so add up the group's largest
        # individual expenditures instead (most_recent skips superseded amendments).
        items = (await _get(client, "schedules/schedule_e/", committee_id=gid, cycle=config.CYCLE, per_page=100,
                            sort="-expenditure_amount", most_recent="true"))["results"]
        totals_by: dict[tuple[str, str], dict] = {}
        for r in items:
            if not r.get("candidate_id") or r.get("memo_code") == "X":
                continue
            row = totals_by.setdefault((r["candidate_id"], r["support_oppose_indicator"]), dict(r, total=0))
            row["total"] += r.get("expenditure_amount") or 0
        spends = sorted(totals_by.values(), key=lambda r: -r["total"])[:30]

        donors: dict[str, dict] = {}  # one line per donor, however many checks they wrote
        for r in rows:
            who = (r.get("contributor_name") or "Unnamed").strip()
            d = donors.setdefault(who.upper(), {
                "name": tidy(who), "amount": 0, "gifts": 0,
                "where": ", ".join(x for x in ((r.get("contributor_city") or "").title(), r.get("contributor_state") or "") if x),
                "employer": tidy(r.get("contributor_employer") or "") if r.get("is_individual") else "",
                "conduit": who.upper() in CONDUITS,
                # money from another FEC committee: link to its own page so people can follow it
                "group": r.get("contributor_id") if re.fullmatch(r"C\d{8}", r.get("contributor_id") or "") else ""})
            d["amount"] += r.get("contribution_receipt_amount") or 0
            d["gifts"] += 1

        return {
            "id": gid, "name": c.get("name") or gid, "website": (c.get("website") or "").lower(),
            "about": _about(c), "treasurer": (c.get("treasurer_name") or "").title(),
            "raised": t.get("receipts") or 0, "spent": t.get("disbursements") or 0,
            "independent": t.get("independent_expenditures") or 0,
            "donors": sorted(donors.values(), key=lambda d: -d["amount"])[:20],
            "spends": [{"cid": r["candidate_id"], "name": display_name(r.get("candidate_name") or ""),
                        "office": {"H": "House", "S": "Senate", "P": "President"}.get(r.get("candidate_office") or "", ""),
                        "state": r.get("candidate_office_state") or "", "district": r.get("candidate_office_district") or "",
                        "side": r["support_oppose_indicator"], "total": r.get("total") or 0}
                       for r in spends if (r.get("total") or 0) > 0],
        }

    return await cache.fetch(key, 24 * 3600, load)


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
            employers = [{"name": tidy(r["employer"]), "total": r["total"]}
                         for r in rows if (r.get("employer") or "").upper().strip(" .") not in NOT_EMPLOYERS][:10]
        groups = await _groups(client, [r["committee_id"] for r in outside if r.get("committee_id")])
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
            "website": website(committees[0].get("website") if committees else ""),
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
                         "total": r["total"] or 0, "about": groups.get(r["committee_id"])}
                        for r in outside if (r["total"] or 0) > 0],
            "employers": employers,
        }

    return await cache.fetch(key, TTL, load)
