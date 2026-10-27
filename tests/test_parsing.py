from datetime import date

from bf import census, civic, congress, factcheck, fec, malegislature
from bf.app import _names


def test_display_name():
    assert fec.display_name("COLLINS, MICHAEL A JR") == "Michael A. Collins Jr."
    assert fec.display_name("MCCLURE, LAMONT") == "Lamont McClure"
    assert fec.display_name("O'ROURKE, BETO MR.") == "Beto O'Rourke"


def test_party():
    assert fec.party("DEMOCRATIC PARTY") == "Democratic"
    assert fec.party(None) == "No party listed"


def test_names_uses_committee_nickname():
    firsts, last = _names("T. Jonathan Ossoff", "JON OSSOFF FOR SENATE")
    assert firsts == ["Jonathan", "Jon"] and last == "Ossoff"


def test_factcheck_needs_first_and_last_name():
    garth = {"claimant": "social media users", "text": "Country singer Garth Brooks was booed off stage", "claimReview": [{}]}
    bob = {"claimant": "Bob Brooks", "text": "Rents doubled", "claimReview": [{}]}
    assert not factcheck.about(garth, ["Bob"], "Brooks")
    assert factcheck.about(bob, ["Bob"], "Brooks")


def test_hours_keeps_upcoming_days():
    text = "\n".join(f"Mon, Oct {d}: 9 am - 5 pm" for d in range(20, 31))
    out = civic._hours(text, date(2026, 10, 27))
    assert out.splitlines()[0].startswith("Mon, Oct 27") and out.endswith("and 1 more day")


def test_census_at_large_and_tidy_address():
    body = {"result": {"addressMatches": [{
        "matchedAddress": "1 MAIN ST, BURLINGTON, VT, 05401",
        "coordinates": {"x": -73.2, "y": 44.5},
        "geographies": {"States": [{"STATE": "50"}], "120th Congressional Districts": [{"GEOID": "5000"}],
                        "Counties": [{"NAME": "Chittenden County"}]},
    }]}}
    p = census.parse(body)
    assert p.state == "VT" and p.district == "00" and p.district_label == "At-large"
    assert p.matched == "1 Main St, Burlington, VT 05401"


def test_state_district_names_match_census():
    from bf.statedata import norm
    assert norm("Barnstable-Dukes-Nantucket District") == norm("Barnstable, Dukes and Nantucket")
    assert norm("Plymouth and Barnstable District") == norm("Plymouth and Barnstable")
    assert norm("First Plymouth & Norfolk") == norm("1st Plymouth and Norfolk District")
    assert norm("3rd Barnstable District") != norm("2nd Barnstable")


def test_split_city_offices_are_merged():
    from bf.statedata import _merge_split_towns
    races = [{"office": "Governor's Council", "district": d, "nominees": [{"name": n}], "contests": [i]}
             for i, (d, n) in enumerate([("3rd", "A"), ("4th", "B")])] + [{"office": "Sheriff", "district": "Suffolk", "nominees": [], "contests": [9]}]
    out = _merge_split_towns(races)
    assert len(out) == 2 and out[0]["split"] == ["3rd", "4th"] and [n["district"] for n in out[0]["nominees"]] == ["3rd", "4th"]


def test_tidy_keeps_acronyms():
    assert fec.tidy("A16Z CAPITAL MANAGEMENT LLC.") == "A16Z Capital Management LLC."
    assert fec.tidy("WINRED") == "Winred"


def test_ocpf_match():
    from bf.ocpf import match
    rows = [{"name": "Richardson, Jack", "office": "3rd Barnstable"}, {"name": "Richardson, John", "office": "5th Essex"},
            {"name": "Healey, Maura T.", "office": "Governor"}]
    assert match(rows, "Jack Peter Richardson", "State Representative", "3rd Barnstable")["office"] == "3rd Barnstable"
    assert match(rows, "Maura Healey", "Governor", "Statewide")["office"] == "Governor"
    assert match(rows, "Nobody Here", "Governor", "Statewide") is None


def test_fec_website_cleanup():
    assert fec.website("HTTPS://ELECTJON.COM/") == "https://electjon.com/"
    assert fec.website("NIKEMAFORCONGRESS.COM") == "https://nikemaforcongress.com"
    assert fec.website("NONE") == "" and fec.website(None) == ""


def test_hand_checked_website_lookup():
    from bf.statedata import website
    assert website("MA", "Jack Peter Richardson", "State Representative", "3rd Barnstable")["url"] == "https://jackprichardson.com/"
    assert website("MA", "Jack Peter Richardson", "State Representative", "2nd Barnstable") is None


def test_ocpf_others_skips_major_parties_and_nominees():
    from bf.ocpf import others
    rows = [{"name": "James, Andrea", "office": "Governor", "party": "None", "raised": 1},
            {"name": "Healey, Maura T.", "office": "Governor", "party": "Democratic", "raised": 1},
            {"name": "Whipps, Susannah M.", "office": "2nd Franklin", "party": "Unenrolled", "raised": 1}]
    gov = others(rows, "Governor", "", ["Maura Healey"])
    assert [o["name"] for o in gov] == ["James, Andrea"] and gov[0]["party"] == "No party listed"
    assert [o["name"] for o in others(rows, "State Representative", "2nd Franklin", [])] == ["Whipps, Susannah M."]
    assert others(rows, "Sheriff", "Barnstable County", []) == []


def test_integrity_slug():
    from bf.integrity import slugify
    assert slugify("Jon", "Ossoff") == "jon-ossoff" and slugify("Ed", "Markey") == "ed-markey"


def test_race_summary_is_built_from_the_numbers():
    from bf.app import race_summary
    a = {"name": "Ann Lee", "raised": 3_000_000, "incumbent": True, "support": 0, "oppose": 500_000}
    b = {"name": "Bo Diaz", "raised": 1_000_000, "incumbent": False, "support": 0, "oppose": 0}
    s = race_summary([a, b])
    assert "Lee has raised the most, $3.0M: about 3 times as much as Diaz." in s
    assert "Ann Lee is the incumbent." in s and "$500K, to oppose Lee" in s
    assert "No outside groups" in race_summary([dict(a, oppose=0), b])


def test_split_city_resolved_by_precinct():
    from bf.census import Place
    from bf.statedata import in_my_precinct
    gc3 = {"precincts": ["BOSTON|21|1", "BOSTON|21|2"]}
    gc4 = {"precincts": ["BOSTON|10|1"]}
    voter = Place(matched="", state="MA", state_name="Massachusetts", district="07", county="", town="Boston city",
                  state_upper="", state_lower="", lat=0, lng=0, precinct_key="BOSTON|21|2")
    assert in_my_precinct(gc3, voter) and not in_my_precinct(gc4, voter)
    elsewhere = Place(**{**voter.__dict__, "precinct_key": "ACTON||1"})
    assert in_my_precinct(gc4, elsewhere)  # Acton isn't split, so the town match decides


def test_head_and_404(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from bf import app as appmod, config
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(appmod.background, "run", lambda *a: __import__("asyncio").sleep(0))
    with TestClient(appmod.app) as client:
        assert client.head("/").status_code == 200
        r = client.get("/no-such-page")
        assert r.status_code == 404 and "Not found" in r.text and "Content-Security-Policy" in r.headers
        assert client.get("/about").headers["cache-control"] == "public, max-age=300"


ROLL = b"""<?xml version="1.0" encoding="utf-8"?>
<rollcall-vote><vote-metadata>
  <rollcall-num>74</rollcall-num><legis-num>H R 4626</legis-num>
  <vote-question>On Motion to Suspend the Rules and Pass, as Amended</vote-question>
  <vote-result>Passed</vote-result><action-date>24-Feb-2026</action-date>
  <vote-desc>Don't Mess With My Home Appliances Act</vote-desc>
</vote-metadata><vote-data>
  <recorded-vote><legislator name-id="A000370">Adams</legislator><vote>Yea</vote></recorded-vote>
  <recorded-vote><legislator name-id="A000148">Auchincloss</legislator><vote>Not Voting</vote></recorded-vote>
</vote-data></rollcall-vote>"""


def test_house_roll_call():
    vote, casts = congress.parse_roll(ROLL)
    assert vote["date"] == "2026-02-24" and vote["final"] and vote["legis"] == "H R 4626"
    assert casts == {"A000370": "Y", "A000148": "-"}
    assert congress.bill_link("H R 4626") == "https://www.congress.gov/bill/119th-congress/house-bill/4626"
    assert congress.bill_link("QUORUM") == "" and congress.bill_name("H J RES 213") == "H.J.Res. 213"
    assert congress.parse_roll(b'<xml>Error sanitizing file "roll363.xml". Please try again.</xml>') is None


def test_session_law_titles():
    assert malegislature.tidy_title("AN ACT AMENDING THE CHARTER OF THE CITY OF MALDEN") == "An Act Amending the Charter of the City of Malden"
    assert malegislature.tidy_title("An Act relative to Bourne") == "An Act relative to Bourne"
    assert malegislature.bill_name("SD4203") == "SD.4203" and malegislature.bill_name("H5640") == "H.5640"
