from datetime import date

from bf import census, civic, factcheck, fec
from bf.app import _names


def test_display_name():
    assert fec.display_name("COLLINS, MICHAEL A JR") == "Michael A. Collins Jr."
    assert fec.display_name("MCCLURE, LAMONT") == "Lamont McClure"
    assert fec.display_name("O'ROURKE, BETO MR.") == "Beto O'Rourke"


def test_party():
    assert fec.party("DEMOCRATIC PARTY") == "Democratic"
    assert fec.party(None) == "No party listed"


def test_names_uses_committee_nickname():
    firsts, last = _names({"name": "T. Jonathan Ossoff", "committee_name": "JON OSSOFF FOR SENATE"})
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
