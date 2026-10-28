"""State and county races from per-state data files in bf/data/ (built by the scripts in tools/).

Each file holds that state's 2026 primary contests. The winners are the party nominees on the
November ballot. An address gets every statewide contest, plus each district contest that covers
its town; legislative seats are matched by the district names the Census returns, because some
towns are split between two districts.
"""
from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from .census import Place

DATA = Path(__file__).parent / "data"

# Congress comes from the FEC, with money attached, so it's left out here.
FEDERAL = {"U.S. Senate", "U.S. House"}
# Ballot order, top to bottom; anything not listed goes after these, alphabetically.
ORDER = ["Governor", "Lieutenant Governor", "Attorney General", "Secretary of the Commonwealth", "Treasurer",
         "Auditor", "Governor's Council", "State Senate", "State Representative", "District Attorney", "Sheriff",
         "Register of Probate", "Clerk of Courts", "Register of Deeds", "County Commissioner", "County Treasurer"]
LEGISLATIVE = {"State Senate": "state_upper", "State Representative": "state_lower"}
ORDINALS = {"first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace(" (write-in)", "")).strip("-")


def norm(district: str) -> str:
    """'First Plymouth & Norfolk' and 'Plymouth and Norfolk District' style names → one comparable form."""
    d = district.lower().replace("&", "and").removesuffix(" district")
    d = re.sub(r"[^a-z0-9 ]", " ", d)  # 'Barnstable-Dukes-Nantucket' = 'Barnstable, Dukes and Nantucket'
    return " ".join(ORDINALS.get(w, w) for w in d.split() if w != "and")


def town_of(place: Place) -> str:
    """Census county-subdivision names: 'Falmouth town', 'Barnstable Town city' → 'falmouth', 'barnstable'."""
    return re.sub(r"\s+(town|city|town city|plantation|township)$", "", place.town.lower()).strip()


def in_my_precinct(contest: dict, place: Place) -> bool:
    """For contests that list the precincts they cover in split towns: is the voter's one of them?
    True when we can't tell (no precinct known, or the voter's town isn't split for this office)."""
    listed = contest.get("precincts")
    if not listed or not place.precinct_key:
        return True
    town = place.precinct_key.split("|")[0]
    if not any(k.startswith(town + "|") for k in listed):
        return True
    return place.precinct_key in listed


def _merge_split_towns(races: list[dict]) -> list[dict]:
    """A big city can sit in several districts for one office (Boston has three Governor's Council
    districts). Without precinct data we can't tell which is the voter's, so show them together,
    each nominee labelled with their district, and say so."""
    out: list[dict] = []
    for r in races:
        prev = out[-1] if out else None
        if prev and prev["office"] == r["office"] and r["district"]:
            if not prev.get("split"):
                prev["split"] = [prev["district"]]
                for n in prev["nominees"]:
                    n["district"] = prev["district"]
                prev["district"] = ""
            prev["split"].append(r["district"])
            prev["nominees"] += [dict(n, district=r["district"]) for n in r["nominees"]]
            prev["contests"] += r["contests"]
        else:
            out.append(r)
    return out


@cache
def load(state: str) -> dict | None:
    path = DATA / f"{state.lower()}-2026.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # Both parties' primaries cover the same seat, but only a contest with votes lists its precincts.
    seat_precincts: dict[tuple[str, str], set[str]] = {}
    for c in data["contests"]:
        seat_precincts.setdefault((c["office"], c["district"]), set()).update(c.get("precincts") or [])
    for c in data["contests"]:
        c["precincts"] = sorted(seat_precincts[(c["office"], c["district"])])
    by_town: dict[str, list[dict]] = {}
    for c in data["contests"]:
        for t in c["towns"]:
            by_town.setdefault(t.lower(), []).append(c)
    data["by_town"] = by_town
    return data


def races(place: Place) -> tuple[list[dict], dict | None]:
    """Races for an address, grouped as one card row per office, plus the data file's details
    (source, fetch date) to show alongside. ([], None) for states without a data file yet."""
    data = load(place.state)
    if not data:
        return [], None
    picked = [c for c in data["contests"] if c["district"] == "Statewide"] + data["by_town"].get(town_of(place), [])
    seats: dict[tuple[str, str], list[dict]] = {}
    for c in picked:
        if c["office"] in FEDERAL:
            continue
        mine = getattr(place, LEGISLATIVE[c["office"]], "") if c["office"] in LEGISLATIVE else ""
        if mine and norm(mine) != norm(c["district"]):
            continue  # the town is split; this is the other half's seat
        if not in_my_precinct(c, place):
            continue  # a split city: this district covers other wards and precincts
        seats.setdefault((c["office"], c["district"]), []).append(c)

    out = _group(seats)
    out = _merge_split_towns(out)
    return out, _source(data)


def overview(state: str) -> tuple[list[dict], dict | None]:
    """Every state and county race in the state, for browsing without an address."""
    data = load(state)
    if not data:
        return [], None
    seats: dict[tuple[str, str], list[dict]] = {}
    for c in data["contests"]:
        if c["office"] not in FEDERAL:
            seats.setdefault((c["office"], c["district"]), []).append(c)
    return _group(seats), _source(data)


def _source(data: dict) -> dict:
    return {"source": data["source"], "fetched": data["fetched"], "about": data["about"],
            "questions": data.get("questions", []), "questions_source": data.get("questions_source", "")}


def _group(seats: dict[tuple[str, str], list[dict]]) -> list[dict]:
    """One row per seat with its party nominees, in ballot order."""
    out = []
    for (office, district), primaries in seats.items():
        nominees = [dict(x, party=p["party"], unopposed=len(p["candidates"]) == 1, contest=p["id"], slug=slug(x["name"]))
                    for p in primaries for x in p["candidates"] if x["won"]]
        if not nominees:
            continue
        out.append({
            "office": office,
            "district": "" if district == "Statewide" else district,
            "nominees": sorted(nominees, key=lambda n: n["party"]),
            "contests": [p["id"] for p in primaries],
        })
    rank = {o: i for i, o in enumerate(ORDER)}
    out.sort(key=lambda r: (rank.get(r["office"], len(ORDER)), r["office"], r["district"]))
    return out


def candidate(state: str, contest_id: int, who: str) -> dict | None:
    """One nominee's page: their primary, the other nominees for the same seat, and the towns it covers."""
    data = load(state)
    if not data:
        return None
    contest = next((c for c in data["contests"] if c["id"] == contest_id), None)
    if not contest:
        return None
    me = next((x for x in contest["candidates"] if slug(x["name"]) == who), None)
    if not me:
        return None
    rivals = [dict(x, party=c["party"], contest=c["id"], slug=slug(x["name"]), unopposed=len(c["candidates"]) == 1)
              for c in data["contests"]
              if c["office"] == contest["office"] and c["district"] == contest["district"] and c["id"] != contest_id
              for x in c["candidates"] if x["won"]]
    return {"state": state, "contest": contest, "me": me, "rivals": rivals,
            "source": data["source"], "fetched": data["fetched"]}


@cache
def _ma_lookup_data() -> dict:
    path = DATA / "ma-wheredoivote.json"
    return json.loads(path.read_text()) if path.exists() else {}


def ma_lookup(place: Place) -> dict | None:
    """The fields Massachusetts's official "Where Do I Vote" form wants, filled in from the address
    the voter already typed (town comes from the Census, so villages like East Falmouth just work)."""
    data = _ma_lookup_data()
    town = town_of(place).upper()
    code = data.get("towns", {}).get(town)
    if place.state != "MA" or not code or not place.street_no or not place.street_name:
        return None
    suffix = place.street_suffix.upper()
    return {"url": data["source"], "fields": {
        "St_no_high": place.street_no[:6], "St_nm": place.street_name.upper()[:25],
        "StSuffix": suffix if suffix in data.get("suffixes", []) else "",
        "Ct_twn_cd": code, "Ct_twn_nm": town, "Zip": place.zip[:5], "hidReset": "0"}}


@cache
def offices() -> dict[str, dict]:
    """Every state's official election office and its main link (tools/fetch_offices.py)."""
    path = DATA / "state-offices.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    for v in data.values():
        links = dict((label, url) for url, label in v["links"])
        v["main"] = links.get("Election information") or (v["links"][0][0] if v["links"] else "")
    return data


@cache
def _websites() -> dict:
    path = DATA / "websites.json"
    return json.loads(path.read_text()) if path.exists() else {}


def website(state: str, name: str, office: str, district: str) -> dict | None:
    """A hand-checked campaign website (bf/data/websites.json), with the evidence for it."""
    for w in _websites().get(state, []):
        if slug(w["name"]) == slug(name) and w["office"] == office and norm(w["district"]) == norm(district):
            return w
    return None
