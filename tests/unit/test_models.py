"""TASK: Tests the request validation and the response structure.
GRADING: Input/Output Design.
"""
from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from app.models import (
    BarChart,
    ChartType,
    NetworkGraph,
    PlanMode,
    QueryPlan,
    QueryRequest,
    RunState,
    Visualization,
    VisualizationResponse,
)

CHANNEL_X = {"field": "phase", "type": "categorical", "title": "Phase"}
CHANNEL_Y = {"field": "trial_count", "type": "quantitative", "title": "Trials", "unit": "trials"}


# ---- QueryRequest -------------------------------------------------------------------------------


def test_request_needs_only_query_and_fills_defaults():
    request = QueryRequest(query="  pembrolizumab trials by phase  ")
    assert request.query == "pembrolizumab trials by phase"
    assert request.max_citations == 10
    assert request.debug is True
    assert request.drug_name is None


@pytest.mark.parametrize("query", ["", "ab", "   a   ", "x" * 501])
def test_request_rejects_query_outside_3_to_500_chars(query):
    with pytest.raises(ValidationError):
        QueryRequest(query=query)


def test_request_without_query_is_rejected():
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"drug_name": "pembrolizumab"})


def test_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": "lung cancer trials", "drug": "x"})


def test_single_name_becomes_a_one_item_list():
    request = QueryRequest(query="trials by phase", drug_name=" Keytruda ", condition="melanoma")
    assert request.drug_name == ["Keytruda"]
    assert request.condition == ["melanoma"]


def test_name_list_is_stripped_and_deduplicated_ignoring_case():
    request = QueryRequest(query="compare", drug_name=["Nivolumab", " nivolumab", "Pembrolizumab"])
    assert request.drug_name == ["Nivolumab", "Pembrolizumab"]


@pytest.mark.parametrize("names", [[], [""], ["a", "  "], ["a", "b", "c", "d", "e", "f"], "   "])
def test_bad_name_lists_are_rejected(names):
    with pytest.raises(ValidationError):
        QueryRequest(query="compare drugs", drug_name=names)


def test_five_names_are_allowed():
    request = QueryRequest(query="compare drugs", condition=["a", "b", "c", "d", "e"])
    assert len(request.condition) == 5


def test_phase_and_status_accept_only_api_enum_values():
    request = QueryRequest(query="trials", trial_phase=["PHASE1", "NA"], status=["RECRUITING"])
    assert [p.value for p in request.trial_phase] == ["PHASE1", "NA"]
    with pytest.raises(ValidationError):
        QueryRequest(query="trials", trial_phase=["Phase 3"])
    with pytest.raises(ValidationError):
        QueryRequest(query="trials", status=["recruiting"])


@pytest.mark.parametrize("field", ["sponsor", "country"])
def test_blank_sponsor_or_country_is_rejected(field):
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": "trials", field: "   "})


def test_year_bounds_follow_the_current_year():
    latest = date.today().year + 5
    assert QueryRequest(query="trials", start_year=1990, end_year=latest).end_year == latest
    for bad in ({"start_year": 1989}, {"end_year": latest + 1}):
        with pytest.raises(ValidationError):
            QueryRequest.model_validate({"query": "trials", **bad})


def test_start_year_after_end_year_is_rejected():
    with pytest.raises(ValidationError):
        QueryRequest(query="trials", start_year=2020, end_year=2015)
    assert QueryRequest(query="trials", start_year=2020, end_year=2020).start_year == 2020


@pytest.mark.parametrize("value", [-1, 51])
def test_max_citations_must_be_0_to_50(value):
    with pytest.raises(ValidationError):
        QueryRequest(query="trials", max_citations=value)


def test_chart_type_must_be_a_known_chart():
    assert QueryRequest(query="trials", chart_type="heatmap").chart_type is ChartType.HEATMAP
    with pytest.raises(ValidationError):
        QueryRequest(query="trials", chart_type="pie_chart")


# ---- QueryPlan ----------------------------------------------------------------------------------


def test_plan_defaults_to_one_series_named_all_with_no_filters():
    plan = QueryPlan(mode="aggregate", group_by=["phase"])
    assert [s.label for s in plan.series] == ["All"]
    assert plan.series[0].filters.model_dump(exclude_none=True) == {}
    assert plan.filters.model_dump(exclude_none=True) == {}


def test_plan_parses_the_example_in_the_spec():
    plan = QueryPlan.model_validate(
        {
            "mode": "network",
            "filters": {"condition": "melanoma", "status": ["RECRUITING"], "start_year_min": 2015},
            "series": [{"label": "Pembrolizumab", "filters": {"drug": "Pembrolizumab"}}],
            "group_by": [],
            "bin": {"field": "enrollment", "bin_width": 50},
            "network": {"source_field": "sponsor", "target_field": "intervention"},
            "sort": "natural",
            "limit": 10,
            "chart_type": "network_graph",
            "search_terms": {"drug": ["Keytruda"]},
            "interpretation": "Sponsors and drugs in melanoma trials.",
            "unsupported_reason": None,
        }
    )
    assert plan.mode is PlanMode.NETWORK
    assert plan.network.max_edges == 60
    assert plan.series[0].filters.drug == "Pembrolizumab"


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "pie"},
        {"mode": "aggregate", "filters": {"gender": "female"}},
        {"mode": "aggregate", "filters": {"status": ["OPEN"]}},
        {"mode": "aggregate", "made_up_key": 1},
    ],
)
def test_plan_rejects_unknown_modes_filters_and_keys(bad):
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(bad)


# ---- Visualization union and response envelope --------------------------------------------------


def test_visualization_is_chosen_by_its_type_field():
    adapter = TypeAdapter(Visualization)
    bar = adapter.validate_python(
        {
            "type": "bar_chart",
            "title": "Pembrolizumab trials by phase",
            "encoding": {"x": CHANNEL_X, "y": CHANNEL_Y},
            "data": [{"id": "d1", "phase": "Phase 2", "trial_count": 12}],
        }
    )
    assert isinstance(bar, BarChart)
    network = adapter.validate_python(
        {
            "type": "network_graph",
            "title": "Sponsors and drugs",
            "encoding": {
                "nodes": {
                    "label": {"field": "label", "type": "categorical", "title": "Name"},
                    "size": CHANNEL_Y,
                    "color": {"field": "type", "type": "categorical", "title": "Kind"},
                },
                "edges": {
                    "weight": {"field": "shared_trials", "type": "quantitative", "title": "Shared trials"}
                },
            },
            "data": {
                "nodes": [
                    {"id": "n1", "label": "Merck", "type": "sponsor", "trial_count": 3},
                    {"id": "n2", "label": "Pembrolizumab", "type": "intervention", "trial_count": 3},
                ],
                "edges": [{"id": "e1", "source": "n1", "target": "n2", "shared_trials": 3}],
            },
        }
    )
    assert isinstance(network, NetworkGraph)
    assert network.encoding.edges.source == "source"


def test_encoding_must_match_the_chart_type():
    adapter = TypeAdapter(Visualization)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "type": "grouped_bar_chart",  # needs a `series` channel
                "title": "t",
                "encoding": {"x": CHANNEL_X, "y": CHANNEL_Y},
                "data": [],
            }
        )
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "pie_chart", "title": "t", "encoding": {}, "data": []})


def test_every_chart_type_has_exactly_one_visualization_model():
    schema = TypeAdapter(Visualization).json_schema()
    assert set(schema["discriminator"]["mapping"]) == {chart.value for chart in ChartType}


def test_unsupported_response_serializes_without_a_visualization():
    response = VisualizationResponse(
        status="unsupported",
        query="show me trials",
        error={"code": "too_vague", "message": "Name a drug, condition or sponsor."},
    )
    body = response.model_dump(mode="json")
    assert body["status"] == "unsupported"
    assert body["visualization"] is None
    assert body["citations"] == {}
    assert body["meta"]["source"] == "clinicaltrials.gov"
    assert body["meta"]["notes"] == []


def test_ok_response_round_trips_through_json():
    response = VisualizationResponse(
        status="ok",
        query="pembrolizumab trials by phase",
        visualization={
            "type": "bar_chart",
            "title": "Pembrolizumab trials by phase",
            "encoding": {"x": CHANNEL_X, "y": CHANNEL_Y},
            "data": [{"id": "d1", "phase": "Phase 2", "trial_count": 12}],
        },
        citations={
            "d1": {
                "total": 12,
                "shown": 1,
                "search_url": "https://clinicaltrials.gov/search?intr=pembrolizumab",
                "items": [
                    {
                        "nct_id": "NCT04003636",
                        "url": "https://clinicaltrials.gov/study/NCT04003636",
                        "bucket_label": "Phase 2",
                        "evidence": [
                            {"field": "protocolSection.designModule.phases", "value": ["PHASE2"]}
                        ],
                    }
                ],
            }
        },
    )
    again = VisualizationResponse.model_validate_json(response.model_dump_json())
    assert again == response
    assert again.citations["d1"].items[0].evidence[0].value == ["PHASE2"]


# ---- RunState -----------------------------------------------------------------------------------


def test_run_state_trace_keeps_step_details_flat():
    state = RunState(request=QueryRequest(query="lung cancer trials by phase"))
    state.record("fetch", 12.345, api_calls=3, tier=1)
    assert state.trace[0].model_dump() == {"step": "fetch", "ms": 12.3, "api_calls": 3, "tier": 1}


def test_run_state_does_not_repeat_a_note():
    state = RunState(request=QueryRequest(query="lung cancer trials by phase"))
    state.note("Trials with no phase are shown as Unknown.")
    state.note("Trials with no phase are shown as Unknown.")
    assert len(state.notes) == 1
