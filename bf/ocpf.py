"""Massachusetts campaign money from the state's Office of Campaign and Political Finance (OCPF).

OCPF covers every state and county candidate (governor down to register of probate). Its API is
public but undocumented beyond https://api.ocpf.us/swagger/v1/swagger.json.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from . import config
from .cache import Cache
from .statedata import norm

API = "https://api.ocpf.us/"
TTL = 6 * 3600

MAJOR = {"Democratic", "Republican"}
STATEWIDE = {"Governor", "Lieutenant Governor", "Attorney General", "Secretary of the Commonwealth", "Treasurer", "Auditor"}
OUTSIDE_FILE = config.DATA / "ma-outside.json"  # refreshed daily by bf.background; the repo copy is the fallback

# Receipt record types worth showing, in OCPF's own categories. Massachusetts itemizes gifts of
# $50 or more; smaller ones are reported only as an aggregate.
SOURCES = [(201, "People ($50 and over)"), (220, "Small donations (under $50)"), (202, "PACs, parties and other committees"),
           (203, "Unions and associations")]


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict | list:
    r = await client.get(API + path, params=params)
    r.raise_for_status()
    return r.json()


async def candidates(client: httpx.AsyncClient, cache: Cache) -> list[dict]:
    """Year-to-date money for every candidate on the ballot this year, in one request."""
    async def load() -> list[dict]:
        rows = await _get(client, f"reports/legislative/race/depository/{config.CYCLE}")
        return [{
            "cpf": r["cpfId"], "name": r["filerName"], "office": r["officeSought"] or "",
            "party": r.get("partyAffiliation") or "",
            "incumbent": bool(r.get("districtCodeHeld")) and r.get("districtCodeHeld") == r.get("districtCodeSought"),
            "raised": r.get("receiptsYtdNumeric") or 0, "spent": r.get("expendituresYtdNumeric") or 0,
            "cash": r.get("currentCashOnHandNumeric") or 0, "through": r.get("bankReportEndDate") or "",
        } for r in rows]

    return await cache.fetch(f"ocpf:depository:{config.CYCLE}", TTL, load)


def match(rows: list[dict], name: str, office: str, district: str) -> dict | None:
    """ElectionStats says 'Jack Peter Richardson', OCPF says 'Richardson, Jack': same last name,
    same first initial, and the same seat when more than one person fits."""
    words = [w for w in name.replace(" (Write-In)", "").replace(".", "").replace(",", "").split()
             if w.lower() not in ("jr", "sr", "ii", "iii", "iv")]
    if len(words) < 2:
        return None
    first, last = words[0].lower(), words[-1].lower()
    found = [r for r in rows if r["name"].split(",")[0].strip().lower() == last
             and r["name"].partition(",")[2].strip().lower()[:1] == first[:1]]
    if len(found) > 1:
        seat = norm(district if district and district != "Statewide" else office)
        found = [r for r in found if norm(r["office"]) == seat]
    return found[0] if len(found) == 1 else None


async def sources(client: httpx.AsyncClient, cache: Cache, cpf: int) -> list[tuple[str, float]]:
    """This year's receipts by OCPF category: one small summary request per category, cached."""
    async def load() -> list[list]:
        out = []
        for type_id, label in SOURCES:
            d = await _get(client, "search/items", CpfId=cpf, SearchTypeCategory="R", RecordTypeId=type_id, PageSize=1,
                           StartDate=f"1/1/{config.CYCLE}", withSummary="true")
            out.append([label, (d.get("summary") or {}).get("total") or 0])
        return out

    return [tuple(x) for x in await cache.fetch(f"ocpf:sources:{config.CYCLE}:{cpf}", TTL, load)]


def others(rows: list[dict], office: str, district: str, nominees: list[str]) -> list[dict]:
    """Candidates filed with OCPF for this seat outside the two major parties. They skip the
    primary, so the primary results don't show them; whether they made the ballot is for the
    official sample ballot to say. County offices are left out: OCPF names only the county."""
    if office in STATEWIDE:
        seat = lambda r: r["office"] == office
    elif district and district != "Statewide" and "County" not in district:
        seat = lambda r: norm(r["office"]) == norm(district)
    else:
        return []
    taken = {n.split()[-1].lower() for n in nominees}
    return [dict(r, party=r["party"] if r["party"] not in ("", "None") else "No party listed")
            for r in rows if seat(r) and r["party"] not in MAJOR and r["name"].split(",")[0].strip().lower() not in taken]


async def independent_spending(client: httpx.AsyncClient) -> dict[str, list[dict]]:
    """OCPF candidate id → independent expenditure PACs that spent for or against them this year,
    added up per group and side, largest first. About 140 requests, a third of a second apart."""
    reports = await _get(client, f"miscreports/iepacs/reports/{config.CYCLE}", PageSize=1000, StartIndex=1)
    totals: dict[tuple[int, int, bool], dict] = {}
    for rep in reports:
        if rep.get("amendedByReportId"):  # a later amendment replaces this report
            continue
        detail = await _get(client, f"report/{rep['reportId']}")
        for e in detail.get("expenditures") or []:
            cand = e.get("relatedCpfId")
            if not cand:
                continue
            amount = float(str(e.get("amount") or "0").replace("$", "").replace(",", "") or 0)
            row = totals.setdefault((cand, rep["cpfId"], bool(e.get("isSupported"))),
                                    {"group": rep["committeeName"].strip(), "group_cpf": rep["cpfId"],
                                     "side": "S" if e.get("isSupported") else "O", "total": 0.0})
            row["total"] += amount
        await asyncio.sleep(0.3)
    out: dict[str, list[dict]] = {}
    for (cand, _, _), row in totals.items():
        out.setdefault(str(cand), []).append(dict(row, total=round(row["total"], 2)))
    for rows in out.values():
        rows.sort(key=lambda r: -r["total"])
    return out


async def refresh_outside(client: httpx.AsyncClient) -> None:
    data = {"fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "outside": await independent_spending(client)}
    OUTSIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTSIDE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(OUTSIDE_FILE)


_outside_cache: tuple[float, dict] = (0.0, {})


def outside(bundled: dict) -> tuple[dict, str]:
    """The newest independent-spending data: today's refresh if there is one, else the copy in the repo."""
    global _outside_cache
    try:
        mtime = OUTSIDE_FILE.stat().st_mtime
        if mtime != _outside_cache[0]:
            _outside_cache = (mtime, json.loads(OUTSIDE_FILE.read_text()))
        return _outside_cache[1]["outside"], _outside_cache[1]["fetched"]
    except (OSError, ValueError, KeyError):
        return bundled.get("outside", {}), bundled.get("outside_fetched", "")


def outside_age_hours() -> float:
    try:
        return (time.time() - OUTSIDE_FILE.stat().st_mtime) / 3600
    except OSError:
        return 1e9
