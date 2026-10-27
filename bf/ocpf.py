"""Massachusetts campaign money from the state's Office of Campaign and Political Finance (OCPF).

OCPF covers every state and county candidate (governor down to register of probate). Its API is
public but undocumented beyond https://api.ocpf.us/swagger/v1/swagger.json.
"""
from __future__ import annotations

import httpx

from . import config
from .cache import Cache
from .statedata import norm

API = "https://api.ocpf.us/"
TTL = 6 * 3600

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
