"""Ballot Facts: the web app. Pages are rendered on the server; there is no JavaScript, no cookies
and no tracking. A voter's address is used for one request and never stored or logged."""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import background, census, civic, config, congress, factcheck, fec, integrity, malegislature, ocpf, offices, openstates, photos, precincts, statedata
from .cache import Cache
from .states import BY_CODE

log = logging.getLogger("bf")
HERE = Path(__file__).parent

# form-action also allows the Massachusetts official polling-place lookup (a button the voter presses).
CSP = ("default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; form-action 'self' https://www.sec.state.ma.us; "
       "base-uri 'none'; frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), interest-cohort=()",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # IPv4 only: the Google key is locked to the server's IPv4 address, and the server also has IPv6.
    app.state.http = httpx.AsyncClient(timeout=20, headers={"User-Agent": config.USER_AGENT},
                                       transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1))
    app.state.cache = Cache(config.DATA / "cache.db")
    jobs = asyncio.create_task(background.run(app.state.http, app.state.cache))
    yield
    jobs.cancel()
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


def from_query(back: str | None) -> str:
    """'?from=…' so the next page can link back to this ballot (a districts-only link)."""
    return "?" + urlencode({"from": back}) if back else ""


def initials(name: str) -> str:
    words = [w for w in name.replace(".", "").split() if w not in ("Jr", "Sr", "II", "III", "IV") and len(w) > 1]
    return (words[0][0] + words[-1][0]).upper() if len(words) > 1 else name[:1].upper()


templates.env.filters["money"] = money
templates.env.filters["initials"] = initials
templates.env.globals["photo"] = photos.url
templates.env.globals["office_about"] = offices.describe
templates.env.filters["backq"] = from_query
templates.env.filters["date"] = nice_date
templates.env.globals.update(config=config, states=BY_CODE)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    """Unknown addresses get the site's own page, not a bare JSON error."""
    return page(request, "missing.html", 404)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    head = request.method == "HEAD"
    if head:  # answer HEAD like GET without the body (uptime checks and link previews use it)
        request.scope["method"] = "GET"
    response = await call_next(request)
    if head:
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        response = Response(status_code=response.status_code, headers=headers)
    response.headers.update(HEADERS)
    # Every GET page is public data, the same for everyone, so a CDN may keep it for five minutes.
    # The address-based ballot (POST) sets its own private header.
    if request.method == "GET" and response.status_code == 200 and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "public, max-age=300"
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


async def _none():
    return None


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
    if place.state == "MA" and (p := precincts.find(place.lat, place.lng)):
        place.precinct_key, place.precinct_name = p.key, p.name
    place_key = f"{place.state}:{place.district}:{place.state_upper}:{place.state_lower}"
    info, legislators = await asyncio.gather(
        _safe(civic.lookup(http, address, place.lat, place.lng), None),
        _safe(openstates.legislators(http, cache, place_key, place.lat, place.lng), []),
    )
    typed_zip = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", address)
    zip_mismatch = bool(typed_zip and place.zip and typed_zip.group(1) != place.zip)
    resp = await _ballot(request, place, info, legislators, official_lookup=statedata.ma_lookup(place),
                         zip_mismatch=zip_mismatch)
    # Built from someone's address: keep it only in their own browser (so Back works), never in shared caches.
    resp.headers["Cache-Control"] = "private, max-age=0"
    return resp


@app.get("/ballot/{state}/{district}", response_class=HTMLResponse)
async def district_ballot(request: Request, state: str, district: str):
    """The same ballot from a link that names only districts and the town, never a street address."""
    state = state.upper()
    if state not in BY_CODE or not re.fullmatch(r"\d\d", district):
        return page(request, "missing.html", 404)
    q = {k: v[:80] for k, v in request.query_params.items() if k in ("town", "county", "up", "lo", "pct", "pn")}
    place = census.Place.from_key(state, district, q)
    http, cache = request.app.state.http, request.app.state.cache
    info, legislators = await asyncio.gather(
        _safe(civic.lookup(http, place.matched, 0, 0), None),  # town-level: official links, no polling places
        _safe(openstates.by_district(http, cache, state, place.state_upper, place.state_lower), []),
    )
    return await _ballot(request, place, info, legislators, by_link=True)


async def _ballot(request: Request, place: census.Place, info, legislators, by_link: bool = False,
                  official_lookup: dict | None = None, zip_mismatch: bool = False) -> HTMLResponse:
    http, cache = request.app.state.http, request.app.state.cache
    house, senate = await asyncio.gather(
        _safe(fec.race(http, cache, "house", place.state, place.district), []),
        _safe(fec.race(http, cache, "senate", place.state), []),
    )
    races = [r for r in (_race("senate", place.state, "", senate), _race("house", place.state, place.district, house)) if r]
    state_races, state_source = statedata.races(place)
    if place.state == "MA" and state_races:
        await _add_ocpf(http, cache, state_races)
    qmoney, qfetched = ocpf.questions(statedata.load("MA") or {}) if place.state == "MA" else ({}, "")
    return page(request, "ballot.html", place=place, info=info, races=races, legislators=legislators,
                state_races=state_races, state_source=state_source, by_link=by_link, back=place.key,
                qmoney=qmoney, qfetched=qfetched, official_lookup=official_lookup, zip_mismatch=zip_mismatch)


async def _add_ocpf(http: httpx.AsyncClient, cache: Cache, state_races: list[dict]) -> None:
    """Money raised, incumbency and photo for each Massachusetts nominee, plus who else filed."""
    rows = await _safe(ocpf.candidates(http, cache), [])
    for r in state_races:
        for n in r["nominees"]:
            m = ocpf.match(rows, n["name"], r["office"], n.get("district") or r["district"] or "Statewide")
            n["raised"] = m["raised"] if m else None
            n["incumbent"] = bool(m and m["incumbent"])
            n["cpf"] = m["cpf"] if m else None
        r["others"] = ocpf.others(rows, r["office"], r["district"], [n["name"] for n in r["nominees"]])


@app.get("/state/{state}", response_class=HTMLResponse)
async def state_page(request: Request, state: str):
    """Everything on the ballot across a state, for people who'd rather not type an address. It
    can't know which district is theirs or where they vote, and says so."""
    state = state.upper()
    if state not in BY_CODE:
        return page(request, "missing.html", 404)
    http, cache = request.app.state.http, request.app.state.cache
    senate, seats = await asyncio.gather(
        _safe(fec.race(http, cache, "senate", state), []),
        _safe(fec.seats(http, cache), []),
    )
    districts = sorted({d for office, st, d in seats if office == "house" and st == state})
    house = []
    for d in districts:
        cands = fec.peek_race(cache, "house", state, d)
        r = _race("house", state, d, cands) if cands else None
        house.append({"district": d, "label": "At-large" if d == "00" else f"District {int(d)}",
                      "path": f"/race/{state}/house/{d}", "race": r, "loaded": cands is not None})
    statewide, legislature, state_source = [], {}, None
    if state == "MA":
        all_races, state_source = statedata.overview(state)
        statewide = [r for r in all_races if not r["district"]]
        await _add_ocpf(http, cache, statewide)
        for r in all_races:
            if r["district"]:
                legislature.setdefault(r["office"], []).append(r)
    qmoney, qfetched = ocpf.questions(statedata.load("MA") or {}) if state == "MA" else ({}, "")
    return page(request, "state.html", state=state, state_name=BY_CODE[state], senate=_race("senate", state, "", senate),
                house=house, seats_known=bool(seats), statewide=statewide, legislature=legislature, state_source=state_source,
                official=statedata.offices().get(state), qmoney=qmoney, qfetched=qfetched)


def back_link(request: Request) -> str:
    """'Back to your ballot' target passed along in ?from=, only ever one of our own ballot links."""
    ref = request.query_params.get("from", "")
    return ref if ref.startswith("/ballot/") and "//" not in ref and len(ref) < 400 else ""


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
            "summary": race_summary(running),
            "office_about": offices.FEDERAL[office],
            # one scale for every bar in the race, so cards compare at a glance
            "outside_total_max": max([c["support"] + c["oppose"] for c in running] + [1])}


def race_summary(running: list[dict]) -> str:
    """One plain sentence or three about the race, built only from the numbers on the page."""
    if not running:
        return ""
    last = lambda c: c["name"].replace(" Jr.", "").replace(" Sr.", "").split()[-1]
    by_money = sorted(running, key=lambda c: -c["raised"])
    top = by_money[0]
    if len(running) == 1:
        out = [f"{top['name']} is the only campaign in this race still reporting to the FEC, with {money(top['raised'])} raised."]
    else:
        second = by_money[1]
        ratio = top["raised"] / max(second["raised"], 1)
        if ratio >= 1.5:
            out = [f"{last(top)} has raised the most, {money(top['raised'])}: about {ratio:.0f} times as much as {last(second)}."]
        else:
            out = [f"{last(top)} ({money(top['raised'])}) and {last(second)} ({money(second['raised'])}) have raised similar amounts."]
    incumbents = [c for c in running if c["incumbent"]]
    if incumbents:
        out.append(f"{incumbents[0]['name']} is the incumbent.")
    spent = sum(c["support"] + c["oppose"] for c in running)
    if not spent:
        out.append("No outside groups have reported spending in this race.")
    else:
        side, c = _biggest_outside(running)
        out.append(f"Outside groups have spent {money(spent)}; the most, {money(c['support'] if side == 'to support' else c['oppose'])}, {side} {last(c)}.")
    return " ".join(out)


def _biggest_outside(running: list[dict]) -> tuple[str, dict]:
    best = max(running, key=lambda c: max(c["support"], c["oppose"]))
    return ("to support", best) if best["support"] >= best["oppose"] else ("to oppose", best)


@app.get("/photo/{name}")
async def photo(name: str):
    if not photos.NAME.fullmatch(name) or not (photos.DIR / name).exists():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(photos.DIR / name, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


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
    return page(request, "race.html", race=r, back=back_link(request))


@app.get("/candidate/{cid}", response_class=HTMLResponse)
async def candidate(request: Request, cid: str):
    if not re.fullmatch(r"[HSP]\d[A-Z0-9]{7}", cid):
        return page(request, "missing.html", 404)
    http, cache = request.app.state.http, request.app.state.cache
    c = await _safe(fec.candidate(http, cache, cid), None)
    if not c:
        return page(request, "missing.html", 404)
    firsts, last = _names(c["name"], c.get("committee_name", ""))
    bioguide = (await _safe(photos.congress_ids(http, cache), {})).get(cid, "")
    checks, ii, bills = await asyncio.gather(
        _safe(factcheck.search(http, cache, firsts, last), []),
        _safe(integrity.profile(http, cache, firsts, last, BY_CODE.get(c["state"], "")), ""),
        _safe(congress.sponsored(http, cache, bioguide), None) if bioguide else _none(),
    )
    if c["office"] == "S":
        race_title, race_path = f"U.S. Senate, {BY_CODE.get(c['state'], c['state'])}", f"/race/{c['state']}/senate"
    else:
        seat = "At-large" if c["district"] in ("", "00") else f"District {int(c['district'])}"
        race_title, race_path = f"U.S. House, {BY_CODE.get(c['state'], c['state'])} {seat}", f"/race/{c['state']}/house/{c['district'] or '00'}"
    raised_total = sum(v for _, v in c["sources"]) or 1
    return page(request, "candidate.html", c=c, checks=checks, back=back_link(request), integrity_url=ii, race_title=race_title, race_path=race_path,
                raised_total=raised_total, bioguide=bioguide, votes=congress.house_record(bioguide) if bioguide else None, bills=bills,
                support=[o for o in c["outside"] if o["side"] == "S"], oppose=[o for o in c["outside"] if o["side"] == "O"])


@app.get("/ma/questions", response_class=HTMLResponse)
async def ma_questions(request: Request):
    """The statewide questions and who's paying, on their own: no address needed, quick on a phone."""
    data = statedata.load("MA") or {}
    qmoney, qfetched = ocpf.questions(data)
    return page(request, "questions.html", state_source={"questions": data.get("questions", []),
                "questions_source": data.get("questions_source", "")}, qmoney=qmoney, qfetched=qfetched)


@app.get("/group/{gid}", response_class=HTMLResponse)
async def group(request: Request, gid: str):
    if not re.fullmatch(r"C\d{8}", gid):
        return page(request, "missing.html", 404)
    g = await _safe(fec.group(request.app.state.http, request.app.state.cache, gid), None)
    if not g:
        return page(request, "missing.html", 404)
    return page(request, "group.html", g=g, back=back_link(request))


@app.get("/{state}/c/{contest_id}/{who}", response_class=HTMLResponse)
async def state_candidate(request: Request, state: str, contest_id: int, who: str):
    d = statedata.candidate(state.upper(), contest_id, who)
    if not d:
        return page(request, "missing.html", 404)
    http, cache = request.app.state.http, request.app.state.cache
    checks = await _safe(factcheck.search(http, cache, *_names(d["me"]["name"])), [])
    money = sources = None
    outside: list[dict] = []
    outside_fetched = ""
    if d["state"] == "MA":
        rows = await _safe(ocpf.candidates(http, cache), [])
        money = ocpf.match(rows, d["me"]["name"], d["contest"]["office"], d["contest"]["district"])
        if money:
            sources = await _safe(ocpf.sources(http, cache, money["cpf"]), None)
            spending, outside_fetched = ocpf.outside(statedata.load("MA") or {})
            outside = spending.get(str(money["cpf"]), [])
    site = statedata.website(d["state"], d["me"]["name"], d["contest"]["office"], d["contest"]["district"])
    office = d["contest"]["office"]
    record = None
    if d["state"] == "MA" and office == "Governor" and money and money["incumbent"]:
        record = {"laws": malegislature.laws()}
    elif d["state"] == "MA" and office in malegislature.BRANCH and (code := malegislature.member_code(d["me"]["name"], office)):
        record = {"member": await _safe(malegislature.member(http, cache, code), None), "senate": malegislature.senate_record(code)}
    return page(request, "state_candidate.html", record=record, d=d, checks=checks, back=back_link(request), site=site, state_name=BY_CODE.get(d["state"], d["state"]),
                money=money, sources=sources, sources_total=sum(v for _, v in sources or []) or 1,
                support=[o for o in outside if o["side"] == "S"], oppose=[o for o in outside if o["side"] == "O"],
                outside_fetched=outside_fetched)


def _names(name: str, committee_name: str = "") -> tuple[list[str], str]:
    """First names a candidate goes by (skipping initials), plus their last name, for fact-check
    searches. A campaign committee's name often has the everyday one: 'JON OSSOFF FOR SENATE'."""
    words = [w for w in name.replace(" (Write-In)", "").replace(".", "").split() if w not in ("Jr", "Sr", "II", "III", "IV")]
    last = words[-1]
    firsts = [w for w in words[:-1] if len(w) > 1][:1]
    committee = committee_name.title().split()
    if last in committee and (i := committee.index(last)) > 0 and committee[i - 1] not in firsts:
        firsts.append(committee[i - 1])
    return firsts or [words[0]], last


@app.get("/results", response_class=HTMLResponse)
async def results(request: Request):
    offices = statedata.offices()
    pick = request.query_params.get("state", "").upper()
    return page(request, "results.html", offices=offices, pick=pick if pick in offices else "", back=back_link(request))


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return page(request, "about.html")


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    # Ballot pages call several outside APIs per view; search engines don't need to crawl them.
    return "User-agent: *\nDisallow: /ballot\n"
