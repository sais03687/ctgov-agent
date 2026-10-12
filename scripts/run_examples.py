"""TASK: Runs the five example questions and saves the real JSON responses.
GRADING: Submission requirement: example runs.

    uv run python scripts/run_examples.py

For each example, `examples/<name>.request.json` is the request body and
`examples/<name>.response.json` is the unedited response of `POST /query`. One example per
chart family: time series, grouped-bar comparison, geographic bar, histogram, network.

Needs OPENAI_API_KEY in .env. `max_citations` is 3 to keep the files readable; the default is 10.
"""
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import app  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "examples"

EXAMPLES = [
    ("01_time_series", {"query": "How has the number of trials for semaglutide changed per year since 2015?", "max_citations": 3}),
    ("02_grouped_bar_comparison", {"query": "Compare phases for trials involving nivolumab vs ipilimumab", "max_citations": 3}),
    ("03_geographic_bar", {"query": "Which countries have the most recruiting trials for melanoma?", "max_citations": 3}),
    ("04_histogram", {"query": "What does the distribution of enrollment sizes look like for cystic fibrosis trials?", "max_citations": 3}),
    ("05_network", {"query": "Which drugs frequently co-occur in combination studies for glioblastoma?", "max_citations": 3}),
]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with TestClient(app) as client:
        for name, body in EXAMPLES:
            response = client.post("/query", json=body)
            data = response.json()
            (OUT / f"{name}.request.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8", newline="\n")
            (OUT / f"{name}.response.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
            )
            viz = data["visualization"]
            points = viz["data"] if isinstance(viz["data"], list) else viz["data"]["nodes"] + viz["data"]["edges"]
            print(
                f"{name}: HTTP {response.status_code} {data['status']} | {viz['type']} | {viz['title']} | "
                f"{len(points)} data points, {len(data['citations'])} citation sets, {data['meta']['trials_matched']:,} trials"
            )


if __name__ == "__main__":
    main()
