"""TASK: Tests the flow from a plan to a response, without an LLM.
GRADING: System Design. Code Quality (correctness).
"""
import json
from collections import Counter

import httpx
import pytest
from pydantic import TypeAdapter

from app.ctgov.client import UNAVAILABLE_MESSAGE
from app.models import BarChart, QueryPlan, QueryRequest, Visualization, VisualizationResponse
from app.pipeline import AGGREGATORS, Pipeline, phase_filter_note, plan_fields
from tests.fakes import VERSION, FakeCtGov, clone_studies, make_client

PEMBRO_BY_PHASE = QueryPlan(
    mode="aggregate",
    filters={"drug": "pembrolizumab"},
    group_by=["phase"],
    sort="natural",
    interpretation="Count pembrolizumab trials in each phase.",
)
REQUEST = QueryRequest(query="pembrolizumab trials by phase")


def pipeline_for(server) -> Pipeline:
    return Pipeline(make_client(server))


@pytest.fixture
def pembro(load_fixture):
    return load_fixture("pembrolizumab_page")["studies"]


# ---- acceptance: "pembrolizumab trials by phase" -> bar chart -----------------------------------


async def test_hand_plan_gives_a_valid_bar_chart_from_the_fixture(pembro):
    response = await pipeline_for(FakeCtGov(pembro)).run_plan(REQUEST, PEMBRO_BY_PHASE)

    body = json.loads(response.model_dump_json())  # exactly what a frontend would receive
    assert VisualizationResponse.model_validate(body) == response
    assert isinstance(TypeAdapter(Visualization).validate_python(body["visualization"]), BarChart)

    assert body["status"] == "ok" and body["error"] is None
    assert body["query"] == "pembrolizumab trials by phase"
    chart = body["visualization"]
    assert chart["type"] == "bar_chart"
    assert chart["title"] == "Pembrolizumab trials by phase"
    assert chart["encoding"]["x"]["field"] == "phase" and chart["encoding"]["y"]["field"] == "trial_count"
    assert all(set(row) == {"id", "phase", "trial_count"} for row in chart["data"])

    # the numbers are the fixture's numbers, counted independently from the raw JSON
    raw = Counter(tuple(s["protocolSection"]["designModule"].get("phases", [])) for s in pembro)
    got = {row["phase"]: row["trial_count"] for row in chart["data"]}
    assert sum(got.values()) == 50
    assert got["Phase 2"] == raw[("PHASE2",)]
    assert got["Phase 1/2"] == raw[("PHASE1", "PHASE2")]
    assert got["Unknown"] == raw[()] == 3
    assert list(got) == [p for p in ["Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4", "Not Applicable", "Unknown"] if p in got]


async def test_meta_tells_the_frontend_what_it_is_looking_at(pembro):
    response = await pipeline_for(FakeCtGov(pembro)).run_plan(REQUEST, PEMBRO_BY_PHASE)
    meta = response.meta
    assert meta.interpretation == "Count pembrolizumab trials in each phase."
    assert meta.filters_applied == {"All": {"drug": "pembrolizumab"}}
    assert meta.plan == PEMBRO_BY_PHASE
    assert (meta.sort, meta.time_granularity, meta.units) == ("natural", None, {"y": "trials"})
    assert (meta.trials_matched, meta.trials_used, meta.tier) == (50, 50, 1)
    assert meta.chart_overridden is False and meta.pruning is None
    assert meta.notes == ['3 trials have no phase listed and are shown as "Unknown".']
    assert meta.source == "clinicaltrials.gov"
    assert meta.data_timestamp == VERSION["dataTimestamp"]
    assert meta.retrieved_at.endswith("+00:00")


async def test_trace_records_each_step_with_its_details(pembro):
    response = await pipeline_for(FakeCtGov(pembro)).run_plan(REQUEST, PEMBRO_BY_PHASE)
    steps = {step.step: step.model_dump() for step in response.meta.trace}
    assert list(steps) == ["fetch", "aggregate", "cite", "build"]
    assert steps["cite"]["data_points"] == len(response.visualization.data)
    assert steps["cite"]["from_sample"] is False
    assert steps["fetch"]["api_calls"] == 3 and steps["fetch"]["cache_hits"] == 0
    assert steps["fetch"]["tier"] == 1 and steps["fetch"]["trials_matched"] == 50
    assert steps["fetch"]["series"] == [{"label": "All", "trials_matched": 50, "tier": 1, "error": None}]
    assert steps["aggregate"]["dropped"] == {} and steps["aggregate"]["trials_used"] == 50
    assert steps["aggregate"]["data_points"] == len(response.visualization.data)
    assert steps["build"] == {"step": "build", "ms": steps["build"]["ms"], "chart": "bar_chart", "chart_overridden": False}
    assert all(step["ms"] >= 0 for step in steps.values())


async def test_debug_false_leaves_the_trace_out(pembro):
    request = QueryRequest(query="pembrolizumab trials by phase", debug=False)
    response = await pipeline_for(FakeCtGov(pembro)).run_plan(request, PEMBRO_BY_PHASE)
    assert response.status == "ok" and response.meta.trace is None


async def test_only_the_planned_fields_are_requested(pembro):
    server = FakeCtGov(pembro)
    request = QueryRequest(query="pembrolizumab trials by phase", max_citations=0)
    await pipeline_for(server).run_plan(request, PEMBRO_BY_PHASE)
    [page] = server.page_requests()
    assert page.url.params["query.intr"] == "pembrolizumab"
    assert plan_fields(PEMBRO_BY_PHASE, with_citations=False) == ["phase"]


async def test_trials_are_built_with_citation_fields_only_when_citations_are_wanted(pembro):
    """The pages always hold all fields. The difference is the list of fields that each run reads from them."""
    wanted = await pipeline_for(FakeCtGov(pembro)).run_plan(REQUEST, PEMBRO_BY_PHASE)
    assert all(item.title for cited in wanted.citations.values() for item in cited.items)
    request = QueryRequest(query="pembrolizumab trials by phase", max_citations=0)
    unwanted = await pipeline_for(FakeCtGov(pembro)).run_plan(request, PEMBRO_BY_PHASE)
    assert all(cited.items == [] for cited in unwanted.citations.values())
    assert [row["trial_count"] for row in wanted.visualization.data] == [row["trial_count"] for row in unwanted.visualization.data]


def test_plan_fields_lists_what_each_mode_reads():
    assert plan_fields(PEMBRO_BY_PHASE) == ["phase"]
    assert plan_fields(QueryPlan(mode="aggregate", group_by=["phase", "country"])) == ["phase", "country"]
    assert plan_fields(QueryPlan(mode="histogram", bin={"field": "enrollment", "bin_width": 50})) == ["enrollment"]
    assert plan_fields(QueryPlan(mode="points", x_field="enrollment", y_field="duration_months")) == ["enrollment", "duration_months"]
    assert plan_fields(QueryPlan(mode="network", network={"source_field": "sponsor", "target_field": "intervention"})) == ["sponsor", "intervention"]
    assert plan_fields(QueryPlan(mode="network", network={"source_field": "intervention", "target_field": "intervention"})) == ["intervention"]
    assert plan_fields(PEMBRO_BY_PHASE, with_citations=True) == ["phase", "brief_title", "sponsor", "last_updated", "enrollment", "has_results"]
    assert plan_fields(QueryPlan(mode="aggregate", group_by=["sponsor"]), with_citations=True) == ["sponsor", "brief_title", "last_updated", "enrollment", "has_results"]


# ---- other chart shapes through the same path ---------------------------------------------------


async def test_start_year_plan_gives_a_zero_filled_time_series(load_fixture):
    studies = load_fixture("lung_recruiting")["studies"]
    plan = QueryPlan(mode="aggregate", filters={"condition": "lung cancer", "status": ["RECRUITING"]}, group_by=["start_year"])
    response = await pipeline_for(FakeCtGov(studies)).run_plan(QueryRequest(query="lung cancer trials per year"), plan)

    chart = response.visualization
    assert chart.type == "time_series" and chart.encoding.x.type == "temporal"
    years = [row["year"] for row in chart.data]
    assert years == list(range(min(years), max(years) + 1))
    raw_years = Counter(int(s["protocolSection"]["statusModule"]["startDateStruct"]["date"][:4]) for s in studies)
    assert {row["year"]: row["trial_count"] for row in chart.data if row["trial_count"]} == dict(raw_years)
    assert response.meta.time_granularity == "year" and response.meta.sort == "natural"
    assert chart.title == "Recruiting lung cancer trials by start year"


async def test_country_plan_carries_the_explode_note(load_fixture):
    studies = load_fixture("lung_recruiting")["studies"]
    plan = QueryPlan(mode="aggregate", filters={"condition": "lung cancer"}, group_by=["country"], limit=5)
    response = await pipeline_for(FakeCtGov(studies)).run_plan(QueryRequest(query="countries for lung cancer"), plan)
    assert response.visualization.type == "bar_chart" and len(response.visualization.data) == 5
    counts = [row["trial_count"] for row in response.visualization.data]
    assert counts == sorted(counts, reverse=True)
    assert (
        "Trials with more than one country are counted once per country, "
        "so the bars add up to more than the 50 trials found." in response.meta.notes
    )
    assert any("only the 5 with the most trials are shown" in note for note in response.meta.notes)


async def test_two_series_plan_gives_a_grouped_bar_chart(pembro, load_fixture):
    lung = load_fixture("lung_recruiting")["studies"]

    def by_drug(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/version"):
            return None
        return FakeCtGov(pembro if request.url.params.get("query.intr") == "pembrolizumab" else lung)(request)

    plan = QueryPlan(
        mode="aggregate", group_by=["phase"],
        series=[{"label": "Pembrolizumab", "filters": {"drug": "pembrolizumab"}}, {"label": "Other", "filters": {"drug": "other"}}],
    )
    response = await pipeline_for(FakeCtGov(override=by_drug)).run_plan(QueryRequest(query="compare phases"), plan)
    chart = response.visualization
    assert chart.type == "grouped_bar_chart" and chart.encoding.series.field == "series"
    assert chart.title == "Trials by phase: Pembrolizumab vs Other"
    assert {row["series"] for row in chart.data} == {"Pembrolizumab", "Other"}
    assert response.meta.trials_matched == 100
    assert set(response.meta.filters_applied) == {"Pembrolizumab", "Other"}


async def test_two_fields_plan_gives_a_stacked_bar_chart(pembro):
    plan = QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab"}, group_by=["phase", "sponsor_class"])
    response = await pipeline_for(FakeCtGov(pembro)).run_plan(REQUEST, plan)
    chart = response.visualization
    assert chart.type == "stacked_bar_chart"
    assert chart.encoding.stack.field == "sponsor_class"
    assert sum(row["trial_count"] for row in chart.data) == 50


async def test_unfitting_chart_suggestion_is_overridden_with_a_note(pembro):
    request = QueryRequest(query="pembrolizumab trials by phase", chart_type="scatter_plot")
    response = await pipeline_for(FakeCtGov(pembro)).run_plan(request, PEMBRO_BY_PHASE)
    assert response.visualization.type == "bar_chart"
    assert response.meta.chart_overridden is True
    assert response.meta.notes[0].startswith("A scatter plot was asked for")
    assert response.meta.trace[-1].model_dump()["chart_overridden"] is True


# ---- failures become envelopes ------------------------------------------------------------------


async def test_zero_trials_is_ok_with_an_empty_chart_and_a_note(load_fixture):
    response = await pipeline_for(FakeCtGov([])).run_plan(REQUEST, PEMBRO_BY_PHASE)
    assert response.status == "ok"
    assert response.visualization.type == "bar_chart" and response.visualization.data == []
    assert response.meta.notes == ["No trials matched these filters."]
    assert response.meta.filters_applied == {"All": {"drug": "pembrolizumab"}}
    assert (response.meta.trials_matched, response.meta.trials_used) == (0, 0)


async def test_api_down_gives_an_error_envelope_with_the_trace(pembro):
    server = FakeCtGov(pembro, override=lambda r: httpx.Response(500))
    response = await pipeline_for(server).run_plan(REQUEST, PEMBRO_BY_PHASE)
    assert response.status == "error" and response.visualization is None
    assert (response.error.code, response.error.message) == ("ctgov_unavailable", UNAVAILABLE_MESSAGE)
    assert response.meta.trace[0].model_dump()["series"][0]["error"] == "HTTP 500, 3 attempts"
    assert response.meta.filters_applied == {"All": {"drug": "pembrolizumab"}}


async def test_too_many_trials_is_unsupported_with_the_count(load_fixture, pembro):
    def count_only(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(200, json=load_fixture("count_lung")) if request.url.params.get("pageSize") == "1" else None

    plan = QueryPlan(mode="aggregate", filters={"condition": "lung cancer"}, group_by=["country"])
    response = await pipeline_for(FakeCtGov(pembro, override=count_only)).run_plan(QueryRequest(query="lung cancer by country"), plan)
    assert response.status == "unsupported" and response.error.code == "too_many_trials"
    assert "14,613 trials matched" in response.error.message and "Narrow the question" in response.error.message


async def test_rejected_search_is_an_error_that_keeps_the_api_reason(pembro):
    server = FakeCtGov(pembro, override=lambda r: httpx.Response(400, text="Unknown area name"))
    response = await pipeline_for(server).run_plan(REQUEST, PEMBRO_BY_PHASE)
    assert response.status == "error" and response.error.code == "request_rejected"
    assert "Unknown area name" in response.error.details[0]


async def test_one_failed_series_leaves_the_others_and_a_note(pembro):
    def fail_b(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(500) if request.url.params.get("query.intr") == "B" else None

    plan = QueryPlan(
        mode="aggregate", group_by=["phase"],
        series=[{"label": "Drug A", "filters": {"drug": "A"}}, {"label": "Drug B", "filters": {"drug": "B"}}],
    )
    response = await pipeline_for(FakeCtGov(pembro, override=fail_b)).run_plan(QueryRequest(query="compare A and B"), plan)
    assert response.status == "ok"
    assert {row["series"] for row in response.visualization.data} == {"Drug A"}
    assert response.meta.notes[0] == (
        f'The "Drug B" series could not be loaded and is left out: {UNAVAILABLE_MESSAGE}'
    )
    assert response.meta.trials_matched == 50


async def test_plan_marked_unsupported_never_reaches_the_api(pembro):
    server = FakeCtGov(pembro)
    plan = QueryPlan(mode="aggregate", unsupported_reason="Trial records cannot say which drug works best.")
    response = await pipeline_for(server).run_plan(QueryRequest(query="which drug works best?"), plan)
    assert response.status == "unsupported"
    assert response.error.message == "Trial records cannot say which drug works best."
    assert server.requests == []


async def test_a_mode_with_no_aggregator_is_unsupported_not_a_crash(pembro, monkeypatch):
    monkeypatch.delitem(AGGREGATORS, "network")
    server = FakeCtGov(pembro)
    plan = QueryPlan(mode="network", network={"source_field": "sponsor", "target_field": "intervention"})
    response = await pipeline_for(server).run_plan(QueryRequest(query="sponsor drug network"), plan)
    assert response.status == "unsupported" and response.error.code == "mode_not_available"
    assert server.requests == []


async def test_an_unexpected_exception_becomes_an_error_envelope(pembro, monkeypatch):
    def explode(series, plan):
        raise RuntimeError("boom")

    monkeypatch.setitem(AGGREGATORS, PEMBRO_BY_PHASE.mode, explode)
    response = await pipeline_for(FakeCtGov(pembro)).run_plan(REQUEST, PEMBRO_BY_PHASE)
    assert response.status == "error" and response.error.code == "internal_error"
    assert "boom" not in response.error.message  # internals stay in the trace
    assert response.meta.trace[-1].model_dump()["error"] == "RuntimeError: boom"


# ---- phase filter note --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phases", "note"),
    [
        (["PHASE1"], "The Phase 1 filter also includes trials listed as Phase 1/2."),
        (["PHASE2"], "The Phase 2 filter also includes trials listed as Phase 1/2 and Phase 2/3."),
        (["PHASE3"], "The Phase 3 filter also includes trials listed as Phase 2/3."),
        (["PHASE1", "PHASE2"], "The Phase 1 and Phase 2 filter also includes trials listed as Phase 2/3."),
        (["PHASE4"], None),  # no trial spans Phase 4 and another phase
        (["EARLY_PHASE1", "NA"], None),
        (["PHASE1", "PHASE2", "PHASE3"], None),  # both spanning groups are fully chosen
        (None, None),
    ],
)
def test_phase_filter_note(phases, note):
    from app.models import PlanFilters

    assert phase_filter_note(PlanFilters(phase=phases)) == note


async def test_phase_filter_keeps_spanning_trials_and_says_so(load_fixture):
    """A Phase 1 filter also gives Phase 1/2 trials, as the API does. The response tells the user."""
    studies = load_fixture("phase12")["studies"]  # real trials, all Phase 1/2
    plan = QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab", "phase": ["PHASE1"]}, group_by=["phase"])
    server = FakeCtGov(studies)
    response = await pipeline_for(server).run_plan(QueryRequest(query="phase 1 pembrolizumab trials"), plan)
    assert server.page_requests()[0].url.params["filter.advanced"] == "(AREA[Phase]PHASE1)"
    assert {row["phase"]: row["trial_count"] for row in response.visualization.data} == {"Phase 1/2": 10}
    assert response.meta.notes == ["The Phase 1 filter also includes trials listed as Phase 1/2."]


async def test_phase_note_is_given_once_when_every_series_shares_the_filter(pembro):
    plan = QueryPlan(
        mode="aggregate", filters={"phase": ["PHASE3"]}, group_by=["status"],
        series=[{"label": "A", "filters": {"drug": "A"}}, {"label": "B", "filters": {"drug": "B"}}],
    )
    response = await pipeline_for(FakeCtGov(pembro)).run_plan(QueryRequest(query="compare A and B"), plan)
    assert response.meta.notes.count("The Phase 3 filter also includes trials listed as Phase 2/3.") == 1


async def test_country_filter_reaches_the_api_as_an_exact_country_match(pembro):
    plan = QueryPlan(mode="aggregate", filters={"condition": "melanoma", "country": "Georgia"}, group_by=["phase"])
    server = FakeCtGov(pembro)
    response = await pipeline_for(server).run_plan(QueryRequest(query="melanoma trials in Georgia by phase"), plan)
    params = server.page_requests()[0].url.params
    assert params["filter.advanced"] == 'AREA[LocationCountry]"Georgia"' and "query.locn" not in params
    assert response.visualization.title == "Melanoma trials in Georgia by phase"
