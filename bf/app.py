"""Ballot Facts: the web app. Pages are rendered on the server; there is no JavaScript, no cookies
and no tracking. A voter's address is used for one request and never stored or logged."""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import census, civic, config, factcheck, fec, openstates, statedata
from .cache import Cache
from .states import BY_CODE

log = logging.getLogger("bf")
HERE = Path(__file__).parent

CSP = ("default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), interest-cohort=()",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=20, headers={"User-Agent": config.USER_AGENT})
    app.state.cache = Cache(config.DATA / "cache.db")
    yield
    await app.state.http.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def money(x: float | int | None) -> str:
    x = x or 0
    if x >= 1_000_000:
        return f"${x / 1_000_000:.1f}M"
    if x >= 10_000:
        return f"${x / 1000:.0f}K"
    return f"${x:,.0f}"


def nice_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return ""
    return f"{d:%b} {d.day}" + ("" if d.year == config.ELECTION_DAY.year else f", {d.year}")


templates.env.filters["money"] = money
templates.env.filters["date"] = nice_date
templates.env.globals.update(config=config, states=BY_CODE)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(HEADERS)
    return response


def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    days = (config.ELECTION_DAY - date.today()).days
    ctx["countdown"] = f"{days} days to go" if days > 1 else "Tomorrow" if days == 1 else "Today" if days == 0 else ""
    return templates.TemplateResponse(request, name, ctx, status_code=status)


async def _safe(coro, default):
    """One source being down shouldn't take the whole page with it."""
    try:
        return await coro
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "")
        log.warning("source failed: %s %s", type(e).__name__, status)
        return default


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return page(request, "home.html")


@app.post("/ballot", response_class=HTMLResponse)
async def ballot(request: Request, address: str = Form("")):
    address = address.strip()[:200]
    http, cache = request.app.state.http, request.app.state.cache
    place = await _safe(census.lookup(http, address), None) if len(address) > 5 else None
    if not place:
        return page(request, "home.html", 422, error="We couldn't find that address. Try the full street address with city and state.")
    place_key = f"{place.state}:{place.district}:{place.state_upper}:{place.state_lower}"
    info, house, senate, legislators = await asyncio.gather(
        _safe(civic.lookup(http, address, place.lat, place.lng), None),
        _safe(fec.race(http, cache, "house", place.state, place.district), []),
        _safe(fec.race(http, cache, "senate", place.state), []),
        _safe(openstates.legislators(http, cache, place_key, place.lat, place.lng), []),
    )
    races = [r for r in (_race("senate", place.state, "", senate), _race("house", place.state, place.district, house)) if r]
    state_races, state_source = statedata.races(place)
    resp = page(request, "ballot.html", place=place, info=info, races=races, legislators=legislators,
                state_races=state_races, state_source=state_source)
    resp.headers["Cache-Control"] = "no-store"  # this page came from someone's address
    return resp


def _race(office: str, state: str, district: str, cands: list[dict]) -> dict | None:
    if not cands:
        return None
    if office == "senate":
        title, path = f"U.S. Senate, {BY_CODE[state]}", f"/race/{state}/senate"
    else:
        seat = "At-large" if district == "00" else f"District {int(district)}"
        title, path = f"U.S. House, {BY_CODE[state]} {seat}", f"/race/{state}/house/{district}"
    running = [c for c in cands if c["running"]]
    return {"title": title, "path": path, "running": running, "others": [c for c in cands if not c["running"]],
            # one scale for every bar in the race, so cards compare at a glance
            "outside_max": max([c["support"] for c in running] + [c["oppose"] for c in running] + [1])}


@app.get("/race/{state}/senate", response_class=HTMLResponse)
@app.get("/race/{state}/house/{district}", response_class=HTMLResponse)
async def race(request: Request, state: str, district: str = ""):
    state = state.upper()
    if state not in BY_CODE or (district and not re.fullmatch(r"\d\d", district)):
        return page(request, "missing.html", 404)
    office = "house" if district else "senate"
    cands = await _safe(fec.race(request.app.state.http, request.app.state.cache, office, state, district), [])
    r = _race(office, state, district, cands)
    if not r:
        return page(request, "missing.html", 404, what="There's no federal race for that seat this year.")
    return page(request, "race.html", race=r)


@app.get("/candidate/{cid}", response_class=HTMLResponse)
async def candidate(request: Request, cid: str):
    if not re.fullmatch(r"[HSP]\d[A-Z0-9]{7}", cid):
        return page(request, "missing.html", 404)
    http, cache = request.app.state.http, request.app.state.cache
    c = await _safe(fec.candidate(http, cache, cid), None)
    if not c:
        return page(request, "missing.html", 404)
    firsts, last = _names(c)
    checks = await _safe(factcheck.search(http, cache, firsts, last), [])
    if c["office"] == "S":
        race_title, race_path = f"U.S. Senate, {BY_CODE.get(c['state'], c['state'])}", f"/race/{c['state']}/senate"
    else:
        seat = "At-large" if c["district"] in ("", "00") else f"District {int(c['district'])}"
        race_title, race_path = f"U.S. House, {BY_CODE.get(c['state'], c['state'])} {seat}", f"/race/{c['state']}/house/{c['district'] or '00'}"
    raised_total = sum(v for _, v in c["sources"]) or 1
    return page(request, "candidate.html", c=c, checks=checks, race_title=race_title, race_path=race_path,
                raised_total=raised_total,
                support=[o for o in c["outside"] if o["side"] == "S"], oppose=[o for o in c["outside"] if o["side"] == "O"])


@app.get("/group/{gid}", response_class=HTMLResponse)
async def group(request: Request, gid: str):
    if not re.fullmatch(r"C\d{8}", gid):
        return page(request, "missing.html", 404)
    g = await _safe(fec.group(request.app.state.http, request.app.state.cache, gid), None)
    if not g:
        return page(request, "missing.html", 404)
    return page(request, "group.html", g=g)


@app.get("/{state}/c/{contest_id}/{who}", response_class=HTMLResponse)
async def state_candidate(request: Request, state: str, contest_id: int, who: str):
    d = statedata.candidate(state.upper(), contest_id, who)
    if not d:
        return page(request, "missing.html", 404)
    words = [w for w in d["me"]["name"].replace(" (Write-In)", "").replace(".", "").split() if w not in ("Jr", "Sr", "II", "III", "IV")]
    firsts = [w for w in words[:-1] if len(w) > 1][:1] or words[:1]
    checks = await _safe(factcheck.search(request.app.state.http, request.app.state.cache, firsts, words[-1]), [])
    return page(request, "state_candidate.html", d=d, checks=checks, state_name=BY_CODE.get(d["state"], d["state"]))


def _names(c: dict) -> tuple[list[str], str]:
    """First names a candidate goes by (skipping initials), plus their last name. The campaign
    committee's name often has the everyday one: 'JON OSSOFF FOR SENATE'."""
    words = [w for w in c["name"].replace(".", "").split() if w not in ("Jr", "Sr", "II", "III", "IV")]
    last = words[-1]
    firsts = [w for w in words[:-1] if len(w) > 1][:1]
    committee = c.get("committee_name", "").title().split()
    if last in committee and (i := committee.index(last)) > 0 and committee[i - 1] not in firsts:
        firsts.append(committee[i - 1])
    return firsts or [words[0]], last


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return page(request, "about.html")


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return "User-agent: *\nDisallow: /ballot\n"
