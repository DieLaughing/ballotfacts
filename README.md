# Ballot Facts

A free, nonpartisan voter guide. Enter your address and see:

- **Where and how to vote:** nearby polling places, early-voting sites and drop boxes, plus official links (registration check, sample ballot, absentee voting).
- **Your U.S. House and Senate races:** who's running, how much each campaign raised and from whom, and Super PAC spending for or against them.
- **Fact checks:** published fact checks of claims by or about each candidate, in the fact-checker's own words.
- **Your state legislators** (current officeholders).

Ballot Facts doesn't endorse candidates, rate claims or call races.

## Privacy, and how to check it

- Your address is sent once to the US Census geocoder and Google's Civic Information API, then discarded. It's posted in the request body (never in the URL), never written to disk, and the server runs with access logging off.
- No cookies, accounts, analytics, ads or JavaScript. Every response carries a `Content-Security-Policy` that blocks scripts and anything loaded from another site.
- The footer of the live site links to the exact commit it's running.

Only public data is cached (races, money, fact checks), in a small SQLite file.

## Data sources

| What | Source |
|---|---|
| Districts (2026 maps) | [US Census geocoder](https://geocoding.geo.census.gov/) |
| Federal candidates, money, outside spending | [OpenFEC](https://api.open.fec.gov/developers/) |
| Polling places, official links | [Voting Information Project](https://www.votinginfoproject.org/) via [Google Civic Information API](https://developers.google.com/civic-information) |
| Fact checks | [Google Fact Check Tools](https://toolbox.google.com/factcheck/apis) (ClaimReview) |
| State legislators | [Open States](https://open.pluralpolicy.com/) |
| Massachusetts campaign money, independent expenditures | [OCPF](https://www.ocpf.us/) API (`api.ocpf.us`); IE PAC spending collected by `tools/fetch_ma.py outside` |
| Massachusetts state & county races, ballot questions | [ElectionStats](https://electionstats.state.ma.us/) primary results + the state's Information for Voters booklet, collected by `tools/fetch_ma.py` into `bf/data/ma-2026.json` |

**Who's "running":** the FEC doesn't mark nominees, but only general-election campaigns file the pre-general report. Candidates whose latest report reaches its closing date are listed first; everyone else who registered for the seat is listed separately. Candidates raising under $5,000 don't file at all, so the official sample ballot is the full list.

## Adding a state

State and county races come from one JSON file per state in `bf/data/`, built by a script in `tools/` from that state's **official** election sources (no third-party aggregators, nothing behind bot protection). `tools/fetch_ma.py` is the template: it records each primary contest with its candidates, winners (the November nominees) and the towns it covers, and `bf/statedata.py` matches an address to them. A new state needs its own fetch script that writes the same shape. Pull requests welcome.

## Links without addresses

A ballot looked up by address is never stored or linked. Pages opened from it carry a `?from=` link to `/ballot/<state>/<district>?town=…&up=…&lo=…`, which rebuilds the same ballot from districts and town only (everyone in those districts shares it). Only polling places need the street address again.

## Run it

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example my.env   # fill in your keys; keep this file private
set -a; . ./my.env; set +a
.venv/bin/uvicorn bf.app:app --reload --port 8796
```

Or with Docker:

```sh
docker build -t ballotfacts --build-arg BF_COMMIT=$(git rev-parse HEAD) .
docker run --rm -p 127.0.0.1:8796:8796 --env-file my.env -v ballotfacts-data:/data ballotfacts
```

Tests: `.venv/bin/pip install pytest && .venv/bin/pytest`

## Corrections

Found something wrong? [Open an issue](https://github.com/DieLaughing/ballotfacts/issues).

## Licence

[AGPL-3.0-or-later](LICENSE). If you run a modified copy for others, you must share your changes.
