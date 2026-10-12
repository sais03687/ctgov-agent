"""TASK: Runs each row of the failure table with the live services and records the results.
GRADING: Code Quality (robustness with real services).

    uv run python evals/manual_failure_runs.py

This complements `tests/failure/test_failure_table.py`, where both services are faked. Here the
real model decides whether a question is vague or misspelled, and the real APIs fail for real:
an unreachable host for "ClinicalTrials.gov down", a wrong key for "LLM provider down". The one
thing still simulated is a single failing series, which is done by a transport that answers 500
for one drug and passes everything else through to the real API.

Needs OPENAI_API_KEY in .env. Makes about 15 model calls and 40 ClinicalTrials.gov calls.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.aggregate.labels import NameLabeler  # noqa: E402
from app.api import app, get_pipeline  # noqa: E402
from app.config import Settings  # noqa: E402
from app.ctgov.client import CtGovClient  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.planner.planner import Planner  # noqa: E402
from app.planner.tools import PlannerTools  # noqa: E402

OUT = Path(__file__).resolve().parent / "manual_failure_runs.md"


class FailOneDrug(httpx.AsyncBaseTransport):
    """The real API, but each search for one drug gets HTTP 500."""

    def __init__(self, drug: str) -> None:
        self._drug = drug.lower()
        self._real = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("query.intr", "").lower() == self._drug:
            return httpx.Response(500, request=request)
        return await self._real.handle_async_request(request)


def pipeline(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> Pipeline:
    settings = settings or Settings()
    client = CtGovClient(settings=settings, transport=transport)
    return Pipeline(client, Planner(PlannerTools(client), settings=settings), NameLabeler(settings=settings))


# (row, situation, expected status, expected content, request body, pipeline factory)
CASES = [
    ("1a", "zero trials, real terms", "ok", 'empty data, note "No trials matched these filters", filters echoed',
     {"query": "How many phase 4 osimertinib trials started each year before 2005?"}, pipeline),
    ("1b", "zero trials, nonsense term", "ok", "the same: an unknown name is searched, not refused",
     {"query": "xyzzyqwerty trials by phase"}, pipeline),
    ("2a", "misspelled drug", "ok", "corrected with count_trials; term in meta.search_terms",
     {"query": "pembrolizumub trials by status"}, pipeline),
    ("2b", "brand name", "ok", "finds the same trials as the generic name",
     {"query": "Keytruda trials by phase"}, pipeline),
    ("2c", "misspelled sponsor", "ok", "corrected with count_trials; term in meta.search_terms",
     {"query": "how many trials does Phizer have in each phase"}, pipeline),
    ("3", "vague question", "unsupported", "asks for a drug, condition or sponsor",
     {"query": "show me trials"}, pipeline),
    ("4", "out of scope", "unsupported", "says registry data cannot answer efficacy",
     {"query": "which drug works best for melanoma?"}, pipeline),
    ("5", "field that does not exist", "unsupported", "explains what cannot be charted (a real model refuses "
     "this itself; the retry-then-fail path needs a stubbed model, see the automated test)",
     {"query": "melanoma trials by the gender of the principal investigator"}, pipeline),
    ("6", "over the cap, not countable", "unsupported", "matched count, advice to narrow, and narrower versions with counts",
     {"query": "Show a network of sponsors and drugs for cancer trials"}, pipeline),
    ("7", "ClinicalTrials.gov down", "error", '"ClinicalTrials.gov didn\'t respond; try again"',
     {"query": "pembrolizumab trials by phase"},
     lambda: pipeline(Settings(ctgov_base_url="http://127.0.0.1:9/api/v2", ctgov_timeout_seconds=3))),
    ("8", "one series fails", "ok", "the other series, and a note naming the failed one",
     {"query": "Compare phases for trials of nivolumab vs ipilimumab"},
     lambda: pipeline(transport=FailOneDrug("ipilimumab"))),
    ("9", "LLM provider down", "error", "the reason",
     {"query": "pembrolizumab trials by phase"},
     lambda: pipeline(Settings(openai_api_key=SecretStr("sk-invalid-key-for-the-failure-check")))),
]


def summarize(body: dict) -> dict:
    """Returns the parts of a response that show how the service handled the condition."""
    meta, viz = body["meta"], body["visualization"]
    plan_steps = [step for step in meta["trace"] or [] if step["step"] == "plan"]
    summary = {
        "status": body["status"],
        "error": body["error"] and {**body["error"], "suggestions": [s["label"] + (f": {s['trials_matched']:,} trials" if s["trials_matched"] is not None else "") for s in body["error"].get("suggestions", [])]},
        "chart": viz and {"type": viz["type"], "title": viz["title"], "data_points": len(viz["data"]) if isinstance(viz["data"], list) else len(viz["data"]["edges"])},
        "trials_matched": meta["trials_matched"],
        "filters_applied": meta["filters_applied"],
        "search_terms": meta["search_terms"],
        "notes": meta["notes"],
        "tool_calls": [
            {"tool": call["name"], "filters": {k: v for k, v in call["arguments"].get("filters", {}).items() if v}, "result": call["result"]}
            for step in plan_steps for call in step.get("tool_calls", [])
        ],
        "trace": [
            {"step": step["step"], **{k: step[k] for k in ("error", "errors", "retries") if step.get(k)},
             **({"series": step["series"]} if step["step"] == "fetch" else {})}
            for step in meta["trace"] or []
        ],
    }
    return {key: value for key, value in summary.items() if value not in (None, [], {})}


def main() -> None:
    lines = [
        "# Failure behaviours: manual runs against the live services",
        "",
        f"Generated by `evals/manual_failure_runs.py` on {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.",
        "Each block is what the service actually returned (shortened to the relevant parts). The same",
        "rows are tested automatically with both services faked in `tests/failure/test_failure_table.py`.",
        "",
    ]
    table = ["| Row | Situation | Expected | Actual | HTTP | As expected |", "|---|---|---|---|---|---|"]
    blocks = []
    with TestClient(app) as http:  # one client, so every call shares one event loop
        responses = []
        for case in CASES:
            made = case[5]()
            app.dependency_overrides[get_pipeline] = lambda: made  # no parameters for FastAPI to inspect
            try:
                responses.append(http.post("/query", json=case[4]))
            finally:
                app.dependency_overrides.clear()
    for (row, situation, expected_status, expected, body, _), response in zip(CASES, responses):
        actual = summarize(response.json())
        ok = actual["status"] == expected_status
        print(f"row {row:3s} {situation:32s} expected {expected_status:11s} got {actual['status']:11s} HTTP {response.status_code}")
        table.append(
            f"| {row} | {situation} | `{expected_status}`: {expected} | `{actual['status']}` | {response.status_code} | {'yes' if ok else '**NO**'} |"
        )
        blocks += [
            f"## Row {row}: {situation}",
            "",
            f"Request: `{json.dumps(body)}`",
            "",
            "```json",
            json.dumps(actual, indent=2, ensure_ascii=False),
            "```",
            "",
        ]
    OUT.write_text("\n".join(lines + table + [""] + blocks), encoding="utf-8", newline="\n")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
