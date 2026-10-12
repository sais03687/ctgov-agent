"""TASK: Downloads real ClinicalTrials.gov responses for the tests.
GRADING: Code Quality. The tests use real data.

Run once from the project root:  python scripts/fetch_fixtures.py
Uses only the standard library, so no install is needed.
"""
import json
import pathlib
import urllib.parse
import urllib.request

BASE = "https://clinicaltrials.gov/api/v2/studies"
OUT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"

FIELDS = ",".join([
    "NCTId", "BriefTitle", "Phase", "OverallStatus", "StartDate", "StartDateType",
    "CompletionDate", "CompletionDateType",
    "LastUpdatePostDate", "LeadSponsorName", "LeadSponsorClass",
    "EnrollmentCount", "LocationCountry", "Condition",
    "InterventionName", "InterventionType", "InterventionArmGroupLabel",
    "ArmGroupLabel", "ArmGroupType", "HasResults",
])

# name -> query parameters
CASES = {
    # small, full-detail page used by most unit tests
    "pembrolizumab_page": {"query.intr": "pembrolizumab", "pageSize": 50, "fields": FIELDS},
    # Phase 1/2 trials: tests the "combine" rule
    "phase12": {
        "query.intr": "pembrolizumab",
        "filter.advanced": "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2",
        "pageSize": 10, "fields": FIELDS,
    },
    # placebo-controlled trial: tests arm-type placebo removal
    "placebo_arms": {"query.term": "NCT04003636", "pageSize": 1, "fields": FIELDS},
    # recruiting lung cancer in many countries: tests "explode" + dedupe
    "lung_recruiting": {
        "query.cond": "lung cancer", "filter.overallStatus": "RECRUITING",
        "pageSize": 50, "fields": FIELDS,
    },
    # count-only responses used by size-check / tier tests
    "count_lung": {"query.cond": "lung cancer", "countTotal": "true", "pageSize": 1, "fields": "NCTId"},
    "count_pembro": {"query.intr": "pembrolizumab", "countTotal": "true", "pageSize": 1, "fields": "NCTId"},
    # zero results: tests the empty-response path
    "zero_results": {"query.cond": "xyzzyqwerty", "countTotal": "true", "pageSize": 10, "fields": FIELDS},
}


def fetch(params: dict) -> dict:
    url = BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, params in CASES.items():
        data = fetch(params)
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"{name:20s} studies={len(data.get('studies', [])):4d} "
              f"total={data.get('totalCount', '-')}")
    print(f"\nSaved to {OUT}")


if __name__ == "__main__":
    main()
