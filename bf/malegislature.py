"""What Massachusetts officeholders have done this session, from the Legislature's public API
(malegislature.gov/api, no key; its robots.txt allows everything).

- Laws the governor signed: the session laws for each year of the session.
- State Senate votes: every roll call. The API lists who voted which way; the bill and the question
  are only in each roll call's PDF, so we read its first lines and keep just that text.
- State House votes aren't in the API (the House publishes them in PDF journals), so state
  representatives get their bills only.
- Bills each legislator filed: their member record, fetched when someone opens their page.

The background job keeps a file of members, laws and Senate votes in the data folder. Nothing is
picked by hand: every law and vote is listed, newest first.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from datetime import datetime

import httpx
from pypdf import PdfReader

from . import config
from .cache import Cache

log = logging.getLogger("bf.malegislature")
COURT = 194  # the 194th General Court, 2025–2026
YEARS = (2025, 2026)
API = "https://malegislature.gov/api"
SITE = "https://malegislature.gov"
FILE = config.DATA / f"ma-legislature-{COURT}.json"
PAUSE = 1  # seconds between requests to the Legislature
SAY = {"Y": "Yes", "N": "No", "-": "Didn't vote"}
SMALL = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "with"}
BRANCH = {"State Senate": "Senate", "State Representative": "House"}


def _load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {"members": {}, "laws": [], "rolls": [], "casts": {}, "bills": {}}


def _save(data: dict) -> None:
    n = len(data["rolls"])
    data["casts"] = {k: v.ljust(n, ".") for k, v in data["casts"].items()}
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(FILE)


def tidy_title(title: str) -> str:
    """Session-law titles come in capitals: 'AN ACT AMENDING THE CHARTER OF THE CITY OF MALDEN'."""
    if title != title.upper():
        return title
    words = title.lower().split()
    return " ".join(w if i and w in SMALL else w[:1].upper() + w[1:] for i, w in enumerate(words))


def roll_heading(pdf: bytes) -> tuple[str, str]:
    """'S.3237 -- Question on passing the bill to be enacted (2/3) Senate' → ('S3237', 'Question on …')."""
    try:
        text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text() or ""
    except Exception:
        return "", ""
    for line in text.splitlines()[1:4]:
        if " -- " in line:
            bill, _, question = line.partition(" -- ")
            return bill.replace(".", "").strip(), re.sub(r"\s+Senate\s*$", "", question).strip()
    return "", ""


async def _get(client: httpx.AsyncClient, path: str):
    r = await client.get(f"{API}/{path}", timeout=60)
    r.raise_for_status()
    return r


async def _members(client: httpx.AsyncClient, data: dict) -> None:
    codes = [m["MemberCode"] for m in (await _get(client, f"GeneralCourts/{COURT}/LegislativeMembers")).json()]
    for code in codes:
        if code in data["members"]:
            continue
        m = (await _get(client, f"GeneralCourts/{COURT}/LegislativeMembers/{code}")).json()
        data["members"][code] = {"name": m["Name"], "branch": m["Branch"], "district": m.get("District") or ""}
        await asyncio.sleep(PAUSE)


async def _laws(client: httpx.AsyncClient, data: dict) -> None:
    laws = []
    for year in YEARS:
        for x in (await _get(client, f"SessionLaws/{year}")).json():
            try:
                signed = datetime.strptime(x.get("ApprovedDate") or "", "%b %d %Y").date().isoformat()
            except ValueError:
                signed = ""
            laws.append({"year": year, "chapter": x["ChapterNumber"], "type": x.get("Type") or "Acts",
                         "title": tidy_title(x.get("Title") or ""), "status": x.get("Status") or "", "signed": signed,
                         "part": "(in part)" in (x.get("Status") or "")})
        await asyncio.sleep(PAUSE)
    data["laws"] = laws


async def _senate_votes(client: httpx.AsyncClient, data: dict) -> int:
    listed = sorted(r["RollCallNumber"] for r in (await _get(client, f"GeneralCourts/{COURT}/Branches/Senate/RollCalls")).json())
    have = {r["roll"] for r in data["rolls"]}
    added = 0
    for roll in (n for n in listed if n not in have):
        detail = (await _get(client, f"GeneralCourts/{COURT}/Branches/Senate/RollCalls/{roll}")).json()
        pdf = await client.get(f"{API}/DownloadRollCall", params={"Branch": "Senate", "generalCourtNumber": COURT, "rollCallNumber": roll}, timeout=60)
        bill, question = roll_heading(pdf.content) if pdf.status_code == 200 else ("", "")
        question = question or detail.get("QuestionMotion") or ""
        if bill and bill not in data["bills"]:
            await asyncio.sleep(PAUSE)
            doc = await client.get(f"{API}/GeneralCourts/{COURT}/Documents/{bill}", timeout=60)
            data["bills"][bill] = doc.json().get("Title", "") if doc.status_code == 200 else ""
        i = len(data["rolls"])
        data["rolls"].append({"roll": roll, "bill": bill, "question": question})
        for key, cast in (("Yeas", "Y"), ("Nays", "N"), ("Absent", "-")):
            for m in detail.get(key) or []:
                code = m.get("MemberCode")
                if code:
                    data["casts"][code] = data["casts"].get(code, "").ljust(i, ".") + cast
        added += 1
        if added % 25 == 0:
            _save(data)
        await asyncio.sleep(PAUSE)
    return added


async def refresh(client: httpx.AsyncClient) -> int:
    """Bring the members, laws and Senate votes up to date, saving as it goes. Returns new votes."""
    data = _load()
    await _members(client, data)
    await _laws(client, data)
    _save(data)
    added = await _senate_votes(client, data)
    _save(data)
    return added


_cached: tuple[float, dict] | None = None


def _data() -> dict:
    global _cached
    try:
        mtime = FILE.stat().st_mtime
    except OSError:
        return _load()
    if not _cached or _cached[0] != mtime:
        _cached = (mtime, _load())
    return _cached[1]


def bill_name(number: str) -> str:
    """'S3237' → 'S.3237', the way the Legislature writes it."""
    return re.sub(r"^([A-Z]+)(\d)", r"\1.\2", number)


def _words(name: str) -> list[str]:
    return [w for w in name.replace(".", "").replace(",", "").replace(" (Write-In)", "").lower().split()
            if w not in ("jr", "sr", "ii", "iii", "iv")]


def member_code(name: str, office: str) -> str:
    """The sitting legislator a candidate is, if any: same last name and first initial, in the
    chamber they're running for; or, for someone moving chambers, the same first and last name."""
    words = _words(name)
    if len(words) < 2:
        return ""
    members = _data()["members"]
    same = lambda m, full: (w := _words(m["name"])) and w[-1] == words[-1] and (w[0] == words[0] if full else w[0][:1] == words[0][:1])
    found = [k for k, m in members.items() if m["branch"] == BRANCH.get(office) and same(m, False)]
    if len(found) != 1:
        found = [k for k, m in members.items() if same(m, True)]
    if len(found) > 1:  # someone who moved chambers mid-session has a record in each
        found = [k for k in found if members[k]["branch"] == BRANCH.get(office)]
    return found[0] if len(found) == 1 else ""


def laws() -> dict:
    rows = sorted(_data()["laws"], key=lambda x: (x["signed"], x["year"], int(re.sub(r"\D", "", x["chapter"]) or 0)), reverse=True)
    for x in rows:
        x["link"] = f"{SITE}/Laws/SessionLaws/{x['type']}/{x['year']}/Chapter{x['chapter']}"
    return {"laws": rows, "part": sum(x["part"] for x in rows)}


def senate_record(code: str) -> dict | None:
    data = _data()
    casts = data["casts"].get(code)
    if not casts:
        return None
    rows = [dict(r, cast=SAY[c], title=data["bills"].get(r["bill"], ""), name=bill_name(r["bill"]),
                 link=f"{SITE}/Bills/{COURT}/{r['bill']}" if r["bill"] else "")
            for r, c in zip(data["rolls"], casts) if c != "."]
    rows.sort(key=lambda r: r["roll"], reverse=True)
    missed = sum(r["cast"] == SAY["-"] for r in rows)
    return {"votes": rows, "total": len(rows), "missed": missed, "missed_pct": round(100 * missed / len(rows), 1) if rows else 0}


async def member(client: httpx.AsyncClient, cache: Cache, code: str) -> dict:
    """A legislator's own record: branch, district, and the bills they filed as lead sponsor."""
    async def load() -> dict:
        m = (await _get(client, f"GeneralCourts/{COURT}/LegislativeMembers/{code}")).json()
        bills = []
        for b in m.get("SponsoredBills") or []:
            number = b.get("BillNumber") or b.get("DocketNumber") or ""
            if not number or (b.get("PrimarySponsor") or {}).get("Id") != code:
                continue
            bills.append({"number": bill_name(number), "title": b.get("Title") or "", "filed": ((b.get("PrimarySponsor") or {}).get("ResponseDate") or "")[:10],
                          "link": f"{SITE}/Bills/{COURT}/{number}"})
        bills.sort(key=lambda b: b["filed"], reverse=True)
        return {"name": m["Name"], "branch": m["Branch"], "district": m.get("District") or "", "bills": bills,
                "cosponsored": len(m.get("CoSponsoredBills") or []), "profile": f"{SITE}/Legislators/Profile/{code}"}

    return await cache.fetch(f"malegislature:member:v2:{COURT}:{code}", 24 * 3600, load)


def age_hours() -> float:
    try:
        return (datetime.now().timestamp() - FILE.stat().st_mtime) / 3600
    except OSError:
        return 1e9
