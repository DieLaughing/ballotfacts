"""Settings, read once from the environment (see .env.example)."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

DATA = Path(os.environ.get("BF_DATA", "data"))
COMMIT = os.environ.get("BF_COMMIT", "dev")
REPO = "https://github.com/DieLaughing/ballotfacts"

GOOGLE_KEY = os.environ.get("GOOGLE_API_KEY", "")
FEC_KEY = os.environ.get("FEC_API_KEY", "DEMO_KEY")
OPENSTATES_KEY = os.environ.get("OPENSTATES_API_KEY", "")

# The election this build is about. Google's id comes from its /elections list.
CYCLE = 2026
ELECTION_DAY = date(2026, 11, 3)
GOOGLE_ELECTION_ID = os.environ.get("BF_GOOGLE_ELECTION_ID", "12000")
# Only candidates in the general election file the FEC pre-general report, which covers
# activity through this date. A report reaching it means the campaign is still on the ballot.
PRE_GENERAL_THROUGH = "2026-10-14"

WARM = os.environ.get("BF_WARM", "1") != "0"  # background pre-loading of federal races (bf/background.py)

USER_AGENT = f"BallotFacts/{COMMIT} (+{REPO})"
