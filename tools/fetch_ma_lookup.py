"""Build bf/data/ma-wheredoivote.json: the town codes and street suffixes the Massachusetts
Secretary of the Commonwealth's "Where Do I Vote" form expects, so Ballot Facts can hand a voter's
address straight to the official lookup (their browser submits it, only when they press the button).

The site blocks automated requests, so this reads a copy of the page saved from a browser
(File > Save Page As on https://www.sec.state.ma.us/WhereDoIVoteMA/):

    python tools/fetch_ma_lookup.py saved-page.html

The town codes are the standard Massachusetts municipal numbers (1-351, alphabetical), which
don't change.
"""
from __future__ import annotations

import html
import json
import re
import sys
from datetime import date
from pathlib import Path

URL = "https://www.sec.state.ma.us/WhereDoIVoteMA/"
OUT = Path(__file__).resolve().parent.parent / "bf" / "data" / "ma-wheredoivote.json"


def main() -> None:
    page = Path(sys.argv[1]).read_text(errors="replace")
    form = re.search(r'<form action="/WhereDoIVoteMA/"[^>]*>(.*?)</form>', page, re.S).group(1)
    towns = {html.unescape(name).strip().upper(): code for code, name in re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', form)}
    suffix_select = re.search(r'id="ddlStreetSuffix"[^>]*>(.*?)</select>', form, re.S).group(1)
    suffixes = [s for s in re.findall(r'value="([^"]*)"', suffix_select) if s]
    OUT.write_text(json.dumps({"source": URL, "saved": date.today().isoformat(), "fields": ["St_no_high", "St_nm", "StSuffix", "Ct_twn_cd", "Ct_twn_nm", "Zip"],
                               "towns": towns, "suffixes": suffixes}, indent=1, sort_keys=True))
    print(f"{len(towns)} towns, {len(suffixes)} suffixes -> {OUT}")


if __name__ == "__main__":
    main()
