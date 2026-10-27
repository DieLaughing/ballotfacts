"""What each office does, in one plain sentence. Written by hand, kept to facts (term length and
the job's main powers), the same wording for every candidate.

Massachusetts entries follow the state constitution and General Laws. Corrections welcome as
GitHub issues.
"""
from __future__ import annotations

FEDERAL = {
    "senate": "Two senators per state, each serving six years. The Senate writes federal laws, confirms federal judges and cabinet members, and approves treaties.",
    "house": "435 members, each serving two years for one district. The House writes federal laws, starts all tax bills, and can impeach federal officials.",
}

MA = {
    "Governor": "The state's chief executive, elected for four years. Signs or vetoes laws, proposes the state budget, runs state agencies, and nominates judges.",
    "Lieutenant Governor": "Elected with the governor for four years. Takes over if the governor can't serve, and presides over the Governor's Council.",
    "Attorney General": "The state's top lawyer, elected for four years. Represents Massachusetts in court and enforces laws on consumer protection, civil rights, the environment and fair labor.",
    "Secretary of the Commonwealth": "Elected for four years. Runs state elections, keeps public records and business filings, and regulates the sale of securities.",
    "Treasurer": "Elected for four years. Manages the state's cash, borrowing and pension investments, and runs the State Lottery.",
    "Auditor": "Elected for four years. Audits state agencies and programs to check that public money is spent properly.",
    "Governor's Council": "Eight elected members, each serving two years. Votes to approve or reject the governor's picks for judges, as well as pardons and commutations.",
    "State Senate": "40 senators, each serving two years. Writes and votes on state laws and the state budget.",
    "State Representative": "160 representatives, each serving two years. Writes and votes on state laws and the state budget.",
    "District Attorney": "The chief prosecutor for criminal cases in the district, elected for four years.",
    "Sheriff": "Elected for six years. Runs the county jail and house of correction.",
    "Register of Probate": "Elected for six years. Runs the records office of the county's Probate and Family Court: wills, estates, divorces, custody and guardianships.",
    "Clerk of Courts": "Elected for six years. Keeps the records of the county's Superior Court.",
    "Register of Deeds": "Elected for six years. Keeps the county's property records, such as deeds and mortgages.",
    "County Commissioner": "Runs county government, where it still exists, including the county budget.",
    "County Treasurer": "Manages the county's money.",
}


def describe(state: str, office: str) -> str:
    return MA.get(office, "") if state == "MA" else ""
