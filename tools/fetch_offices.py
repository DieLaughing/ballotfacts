"""Build bf/data/state-offices.json: each state's official election office and its links, from
Google's Civic Information API (Voting Information Project data). Needs GOOGLE_API_KEY.

    python tools/fetch_offices.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bf.civic import LINKS  # noqa: E402
from bf.states import STATES  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "bf" / "data" / "state-offices.json"


def main() -> None:
    key = os.environ["GOOGLE_API_KEY"]
    out = {}
    with httpx.Client(timeout=30) as client:
        for code, name in sorted(STATES.values()):
            r = client.get("https://www.googleapis.com/civicinfo/v2/voterinfo",
                           params={"key": key, "address": f"{name}, USA", "electionId": 12000})
            body = (r.json().get("state") or [{}])[0] if r.status_code == 200 else {}
            eab = body.get("electionAdministrationBody") or {}
            out[code] = {"name": name, "office": eab.get("name") or f"{name} election office",
                         "links": [[eab[k], label] for k, label in LINKS if eab.get(k)]}
            print(code, r.status_code, len(out[code]["links"]), file=sys.stderr)
            time.sleep(0.2)
    OUT.write_text(json.dumps(out, indent=1))
    print(f"wrote {OUT}: {sum(1 for v in out.values() if v['links'])}/{len(out)} states with links", file=sys.stderr)


if __name__ == "__main__":
    main()
