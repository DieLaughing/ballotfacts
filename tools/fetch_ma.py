"""Build bf/data/ma-2026.json from the Massachusetts Secretary of the Commonwealth's ElectionStats site.

Each 2026 state primary contest becomes one record: the office, the district, the party, every
candidate with their votes, who won (the party's nominee on the November ballot), and the towns
the district covers. Re-run it any time to check the data against the state's site:

    python tools/fetch_ma.py

It makes about 500 requests, one every half second. ElectionStats' robots.txt allows this.

The statewide ballot questions come from the state's official Information for Voters booklet
(PDF): each question's title and the "what your vote will do" statements written jointly by the
Attorney General and the Secretary of the Commonwealth. Needs `pdftotext` (poppler).
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = "https://electionstats.state.ma.us"
PRIMARY = "2026-09-01"
OUT = Path(__file__).resolve().parent.parent / "bf" / "data" / "ma-2026.json"
NOT_CANDIDATES = {"All Others", "Blanks", "Total Votes Cast", ""}
BOOKLET = "https://www.sec.state.ma.us/divisions/elections/download/information-for-voters/archive/IFV_2026%20English.pdf"


def text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def contests(client: httpx.Client) -> list[dict]:
    page = client.get(f"{BASE}/elections/search/date:{PRIMARY}").text
    out = []
    for cid, row in re.findall(r'<tr[^>]*id="election-id-(\d+)"[^>]*>(.*?)</tr>', page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        summary = re.sub(r"Candidates ».*", "", text(cells[4])).strip()
        out.append({
            "id": int(cid),
            "office": text(cells[1]),
            "district": text(cells[2]),
            "party": text(cells[3]).removesuffix(" Primary"),
            # 'X won …' names the nominee(s); 'X (Write-In) ran against …' means no one qualified
            "won_text": summary.split(" won")[0] if " won" in summary else "",
        })
    return out


def results(client: httpx.Client, cid: int) -> tuple[list[dict], list[str]]:
    rows = list(csv.reader(io.StringIO(client.get(f"{BASE}/elections/download/{cid}/precincts_include:0/").text)))
    if not rows:
        return [], []
    names = rows[0]
    totals = next((r for r in rows if r and r[0] == "TOTALS"), None)
    towns = [r[0] for r in rows[1:] if r and r[0] not in ("", "TOTALS")]
    cands = []
    for i, name in enumerate(names):
        if i == 0 or name.strip() in NOT_CANDIDATES:
            continue
        raw = (totals[i] if totals and i < len(totals) else "").replace(",", "")
        votes = int(raw) if raw.isdigit() else 0  # '-' when nobody ran
        cands.append({"name": name.strip(), "votes": votes})
    return cands, towns


def questions(client: httpx.Client) -> list[dict]:
    """The booklet lays each "WHAT YOUR VOTE WILL DO" box out in two columns (yes, then no);
    pdftotext -layout keeps the columns, so each one is cut out by its starting position."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf:
        pdf.write(client.get(BOOKLET).content)
        pdf.flush()
        lines = subprocess.run(["pdftotext", "-layout", pdf.name, "-"], capture_output=True, text=True, check=True).stdout.splitlines()
    titles = {}
    for line in lines:
        if m := re.match(r"\s*Question (\d+) - (.+?)\.*\s+\d+$", line):
            titles.setdefault(int(m.group(1)), m.group(2).strip(" ."))
    out = []
    starts = [i for i, l in enumerate(lines) if "A YES VOTE" in l and "A NO VOTE" in l]
    for n, i in enumerate(starts, 1):
        yes_at, no_at = lines[i].index("A YES VOTE"), lines[i].index("A NO VOTE")
        yes, no = [], []
        for line in lines[i:]:
            if "STATEMENT" in line[:yes_at]:
                break
            yes.append(line[yes_at:no_at].strip())
            no.append(line[no_at:].strip())
        out.append({"number": n, "title": titles.get(n, f"Question {n}"),
                    "yes": " ".join(w for w in yes if w), "no": " ".join(w for w in no if w)})
    return out


def main() -> None:
    client = httpx.Client(timeout=30, follow_redirects=True,
                          headers={"User-Agent": "BallotFacts data fetch (+https://github.com/DieLaughing/ballotfacts)"})
    found = contests(client)
    print(f"{len(found)} contests", file=sys.stderr)
    for n, c in enumerate(found, 1):
        cands, towns = results(client, c["id"])
        total = sum(x["votes"] for x in cands) or 1
        for x in cands:
            x["pct"] = round(100 * x["votes"] / total)
            x["won"] = bool(c["won_text"]) and x["name"].replace(" (Write-In)", "") in c["won_text"]
        c["candidates"] = cands
        c["towns"] = [] if c["district"] == "Statewide" else towns
        del c["won_text"]
        if n % 50 == 0:
            print(f"  {n}/{len(found)}", file=sys.stderr)
        time.sleep(0.5)
    qs = questions(client)
    print(f"{len(qs)} ballot questions", file=sys.stderr)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "state": "MA",
        "source": f"{BASE}/elections/search/date:{PRIMARY}",
        "about": "Massachusetts 2026 state primary results (Sept 1). Winners are the party nominees on the Nov 3 ballot.",
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "questions_source": BOOKLET,
        "questions": qs,
        "contests": found,
    }, indent=1))
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
