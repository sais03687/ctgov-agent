"""TASK: Gives a score to the planner LLM on a list of questions.
GRADING: AI / Agent Design. Shows the accuracy, speed, and cost of each model.

    uv run python evals/run_evals.py                     # the model in .env (PLANNER_MODEL)
    uv run python evals/run_evals.py --model gpt-5.4     # another model
    uv run python evals/run_evals.py --table             # comparison table from saved results

Only planning is scored: question -> plan -> request overrides -> validation (with the one
retry), exactly as the service does it, but without fetching trials. That is the part a model
choice affects; everything after the plan is deterministic code with its own tests.

A question passes when every expected PROPERTY of the plan holds (mode, fields, series, filters,
chart type) and the plan has no filter that was not asked for. Wording is not compared.

Results go to `evals/results_<model>.json`. Cost is an estimate: measured tokens times the list
prices in `PRICES`, which were taken from public price listings in October 2026 and should be
checked against the provider's own page.
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import spec_builder  # noqa: E402
from app.config import Settings  # noqa: E402
from app.ctgov.client import CtGovClient  # noqa: E402
from app.models import AggregateResult, QueryPlan, QueryRequest, RunState, VisualizationResponse  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.planner.planner import Planner  # noqa: E402
from app.planner.tools import PlannerTools  # noqa: E402

HERE = Path(__file__).resolve().parent
# US dollars per million tokens (input, output). Estimates; see the module docstring.
PRICES = {"gpt-5.4-nano": (0.20, 1.25), "gpt-5.4-mini": (0.75, 4.50), "gpt-5.4": (2.50, 15.00)}
Check = tuple[str, bool, str]


def _contains(expected: Any, actual: Any) -> bool:
    """For text: True if the value contains one of the permitted spellings. For all other types: True if equal."""
    if isinstance(expected, str) or (isinstance(expected, list) and isinstance(actual, str)):
        options = [expected] if isinstance(expected, str) else expected
        return isinstance(actual, str) and any(option.casefold() in actual.casefold() for option in options)
    if isinstance(expected, list):
        return isinstance(actual, list) and sorted(map(str, expected)) == sorted(map(str, actual))
    return expected == actual


def check_plan(expect: dict[str, Any], plan: QueryPlan) -> list[Check]:
    """Compares a plan with the expected properties. Returns one entry for each property."""
    data = plan.model_dump(mode="json")
    filters = {key: value for key, value in data["filters"].items() if value not in (None, [])}
    checks: list[Check] = []

    def add(name: str, ok: bool, got: Any) -> None:
        checks.append((name, bool(ok), json.dumps(got, ensure_ascii=False, default=str)))

    add("mode", data["mode"] == expect["mode"], data["mode"])
    if "group_by" in expect:
        add("group_by", sorted(data["group_by"]) == sorted(expect["group_by"]), data["group_by"])
    add("series", len(data["series"]) == expect.get("series", 1), len(data["series"]))

    wanted = expect.get("filters", {})
    for key, value in wanted.items():
        add(f"filter {key}", _contains(value, filters.get(key)), filters.get(key))
    invented = sorted(set(filters) - set(wanted))
    add("no filter that was not asked for", not invented, invented)

    for key, terms in expect.get("series_terms", {}).items():
        actual = [series["filters"].get(key) for series in data["series"]]
        found = all(any(_contains(term, value) for value in actual) for term in terms)
        add(f"series by {key}", found, actual)
        extra = sorted({k for series in data["series"] for k, v in series["filters"].items() if v not in (None, []) and k != key})
        add("no series filter that was not asked for", not extra, extra)

    if "network" in expect:
        got = [data["network"]["source_field"], data["network"]["target_field"]] if data["network"] else None
        add("network fields", got is not None and sorted(got) == sorted(expect["network"]), got)
    if "bin_field" in expect:
        got = data["bin"]["field"] if data["bin"] else None
        add("bin field", got == expect["bin_field"], got)
    if "x_y" in expect:
        got = [data["x_field"], data["y_field"]]
        add("x and y fields", None not in got and sorted(got) == sorted(expect["x_y"]), got)
    if "start_end" in expect:
        got = [data["start_field"], data["end_field"]]
        add("start and end fields", got == expect["start_end"], got)
    for key in ("sort", "limit"):
        if key in expect:
            allowed = expect[key] if isinstance(expect[key], list) else [expect[key]]
            add(key, data[key] in allowed, data[key])

    chart = spec_builder.default_chart(plan, AggregateResult(mode=plan.mode)).value
    add("chart", chart in expect["chart"], chart)
    return checks


async def run_question(pipeline: Pipeline, question: dict[str, Any]) -> dict[str, Any]:
    state = RunState(request=QueryRequest(query=question["query"]))
    started = time.perf_counter()
    outcome = await pipeline.plan(state)
    seconds = time.perf_counter() - started

    plan_steps = [step.model_dump() for step in state.trace if step.step == "plan"]
    usage = {
        "llm_calls": sum(step.get("llm_calls", 0) for step in plan_steps),
        "tool_calls": sum(len(step.get("tool_calls", [])) for step in plan_steps),
        "retries": max((step.get("retries", 0) for step in plan_steps), default=0),
        "input_tokens": sum(step.get("input_tokens", 0) for step in plan_steps),
        "output_tokens": sum(step.get("output_tokens", 0) for step in plan_steps),
    }
    expect = question["expect"]
    plan: QueryPlan | None = None
    if isinstance(outcome, VisualizationResponse):
        status, detail = outcome.status.value, outcome.error.message if outcome.error else ""
        refused_by_model = outcome.error is not None and outcome.error.code == "unsupported_question"
        plan = state.plan
    elif outcome.unsupported_reason:
        # `Pipeline.plan` hands back the model's refusal as a plan; the service turns it into an
        # `unsupported` response at the start of execution, before anything is fetched
        status, detail, refused_by_model, plan = "unsupported", outcome.unsupported_reason, True, outcome
    else:
        status, detail, refused_by_model, plan = "plan", "", False, outcome

    checks: list[Check] = [("status", status == expect["status"], json.dumps(status))]
    if expect["status"] == "plan" and status == "plan":
        checks += check_plan(expect, plan)
    elif expect["status"] == "unsupported" and status == "unsupported":
        checks.append(("refused by the model with a reason", refused_by_model and bool(detail), json.dumps(detail[:160])))
    return {
        "id": question["id"],
        "group": question["group"],
        "class": question["class"],
        "query": question["query"],
        "passed": all(ok for _, ok, _ in checks),
        "seconds": round(seconds, 2),
        **usage,
        "checks": [{"check": name, "ok": ok, "got": got} for name, ok, got in checks],
        "status": status,
        "detail": detail,
        "plan": plan.model_dump(mode="json", exclude_defaults=True) if plan and status == "plan" else None,
    }


def cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICES:
        return None
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def summarize(model: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {}
    for group in dict.fromkeys(r["group"] for r in results):
        members = [r for r in results if r["group"] == group]
        groups[group] = {"passed": sum(r["passed"] for r in members), "of": len(members)}
    tokens_in = sum(r["input_tokens"] for r in results)
    tokens_out = sum(r["output_tokens"] for r in results)
    total_cost = cost(model, tokens_in, tokens_out)
    seconds = [r["seconds"] for r in results]
    return {
        "model": model,
        "passed": sum(r["passed"] for r in results),
        "of": len(results),
        "groups": groups,
        "seconds_median": round(statistics.median(seconds), 2),
        "seconds_max": round(max(seconds), 2),
        "llm_calls": sum(r["llm_calls"] for r in results),
        "tool_calls": sum(r["tool_calls"] for r in results),
        "retries": sum(r["retries"] for r in results),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "estimated_cost_usd": None if total_cost is None else round(total_cost, 4),
        "estimated_cost_per_question_usd": None if total_cost is None else round(total_cost / len(results), 5),
        "failed": [r["id"] for r in results if not r["passed"]],
    }


async def evaluate(model: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    settings = Settings(planner_model=model)
    client = CtGovClient(settings=settings)
    pipeline = Pipeline(client, Planner(PlannerTools(client), settings=settings))
    results = []
    try:
        for question in questions:
            result = await run_question(pipeline, question)
            results.append(result)
            failed = "; ".join(f"{c['check']}: got {c['got']}" for c in result["checks"] if not c["ok"])
            print(f"  {'PASS' if result['passed'] else 'FAIL'} {result['id']:3s} {result['seconds']:5.1f}s  {result['class']}" + (f"  <- {failed}" if failed else ""))
    finally:
        await client.aclose()
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reasoning_effort": settings.planner_reasoning_effort,
        "summary": summarize(model, results),
        "results": results,
    }


def comparison_table() -> str:
    """Returns a Markdown table of all saved result files. The model with the lowest cost is first."""
    rows = []
    for path in sorted(HERE.glob("results_*.json")):
        s = json.loads(path.read_text(encoding="utf-8"))["summary"]
        groups = " · ".join(f"{name} {g['passed']}/{g['of']}" for name, g in s["groups"].items())
        price = "n/a" if s["estimated_cost_per_question_usd"] is None else f"${s['estimated_cost_per_question_usd']:.4f}"
        rows.append((PRICES.get(s["model"], (99, 99))[0], (
            f"| `{s['model']}` | {s['passed']}/{s['of']} ({100 * s['passed'] / s['of']:.0f}%) | {groups} | "
            f"{s['seconds_median']:.1f} s | {s['seconds_max']:.1f} s | {s['retries']} | {s['tool_calls']} | "
            f"{s['input_tokens'] // s['of']:,} / {s['output_tokens'] // s['of']:,} | {price} | {', '.join(s['failed']) or 'none'} |"
        )))
    header = [
        "| Model | Passed | By group | Median time | Slowest | Retries | Tool calls | Tokens per question (in / out) | Est. cost per question | Failed |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    return "\n".join(header + [row for _, row in sorted(rows)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", default=None, help="planner model to evaluate (default: PLANNER_MODEL)")
    parser.add_argument("--only", nargs="*", help="question ids to run, e.g. A1 B3")
    parser.add_argument("--table", action="store_true", help="print the comparison table from saved results and exit")
    args = parser.parse_args()
    if args.table:
        print(comparison_table())
        return

    model = args.model or Settings().planner_model
    questions = yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]
    print(f"{model}: {len(questions)} questions")
    report = asyncio.run(evaluate(model, questions))
    summary = report["summary"]
    print(
        f"\n{model}: {summary['passed']}/{summary['of']} passed | median {summary['seconds_median']} s | "
        f"tokens {summary['input_tokens']:,} in / {summary['output_tokens']:,} out | "
        f"estimated cost ${summary['estimated_cost_usd']}"
    )
    if not args.only:
        out = HERE / f"results_{model}.json"
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
