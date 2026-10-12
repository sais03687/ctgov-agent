"""TASK: Tests the flow from a question to a response, with a fake LLM.
GRADING: AI / Agent Design (the retry, and no LLM numbers in the response).
"""
import httpx
import openai
import pytest

from app.config import Settings
from app.models import QueryPlan, QueryRequest
from app.pipeline import BOTH_LISTS_MESSAGE, Pipeline, apply_request_filters, canonical
from app.planner.planner import Planner
from app.planner.tools import PlannerTools
from tests.fakes import FakeCtGov, FakeLLM, llm_plan, llm_text, llm_tools, make_client

GOOD = QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab"}, group_by=["phase"], sort="natural", interpretation="Count by phase.")
BAD = QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab"}, group_by=["gender"])
REQUEST = QueryRequest(query="pembrolizumab trials by phase")


@pytest.fixture
def pembro(load_fixture):
    return load_fixture("pembrolizumab_page")["studies"]


def pipeline_with(llm, server) -> Pipeline:
    client = make_client(server)
    return Pipeline(client, Planner(PlannerTools(client), llm=llm, settings=Settings(_env_file=None)))


def steps(response) -> list[str]:
    return [step.step for step in response.meta.trace]


# ---- the normal path ----------------------------------------------------------------------------


async def test_question_goes_from_plan_to_chart(pembro):
    response = await pipeline_with(FakeLLM(llm_plan(GOOD)), FakeCtGov(pembro)).run(REQUEST)
    assert response.status == "ok" and response.visualization.type == "bar_chart"
    assert response.meta.plan == GOOD and response.meta.interpretation == "Count by phase."
    assert steps(response) == ["plan", "validate", "fetch", "aggregate", "cite", "build"]
    plan_step = response.meta.trace[0].model_dump()
    assert (plan_step["model"], plan_step["retries"], plan_step["llm_calls"], plan_step["tool_calls"]) == ("gpt-5.4-mini", 0, 1, [])
    assert (plan_step["input_tokens"], plan_step["output_tokens"]) == (100, 20)
    assert response.meta.trace[1].model_dump()["errors"] == []


async def test_tool_calls_appear_in_the_trace(pembro):
    llm = FakeLLM(llm_tools(("count_trials", {"filters": {"drug": "pembrolizumab"}})), llm_plan(GOOD))
    response = await pipeline_with(llm, FakeCtGov(pembro)).run(REQUEST)
    [call] = response.meta.trace[0].model_dump()["tool_calls"]
    assert (call["name"], call["arguments"], call["result"]) == (
        "count_trials", {"filters": {"drug": "pembrolizumab"}}, {"total_count": 50},
    )


async def test_the_llm_never_supplies_a_number_title_or_citation(pembro):
    """P1: the text of the model does not matter. Counts and citations come from the fetched trials."""
    lying = GOOD.model_copy(update={"interpretation": "There are 9,999 Phase 3 trials, see NCT99999999."})
    response = await pipeline_with(FakeLLM(llm_plan(lying)), FakeCtGov(pembro)).run(REQUEST)
    assert sum(row["trial_count"] for row in response.visualization.data) == 50
    assert response.visualization.title == "Pembrolizumab trials by phase"
    cited = {item.nct_id for c in response.citations.values() for item in c.items}
    fixture_ids = {s["protocolSection"]["identificationModule"]["nctId"] for s in pembro}
    assert cited and cited <= fixture_ids


# ---- P2: one retry, then unsupported ------------------------------------------------------------


async def test_invalid_plan_is_retried_once_with_the_errors_and_then_runs(pembro):
    llm = FakeLLM(llm_plan(BAD), llm_plan(GOOD))
    response = await pipeline_with(llm, FakeCtGov(pembro)).run(REQUEST)
    assert response.status == "ok"
    assert steps(response) == ["plan", "validate", "plan", "validate", "fetch", "aggregate", "cite", "build"]
    first_check, second_check = (response.meta.trace[i].model_dump()["errors"] for i in (1, 3))
    assert first_check[0]["path"] == "group_by[0]" and second_check == []
    assert [response.meta.trace[i].model_dump()["retries"] for i in (0, 2)] == [0, 1]
    assert '"gender" is not a known field' in llm.requests[1]["input"][-1]["content"]


async def test_plan_still_invalid_after_the_retry_is_unsupported_and_nothing_is_fetched(pembro):
    server = FakeCtGov(pembro)
    llm = FakeLLM(llm_plan(BAD))
    response = await pipeline_with(llm, server).run(REQUEST)
    assert response.status == "unsupported" and response.error.code == "plan_invalid"
    assert response.error.message.startswith("This question could not be turned into a query that can be run.")
    assert len(response.error.details) == 1 and '"gender" is not a known field' in response.error.details[0]
    assert len(llm.requests) == 2  # exactly one retry, never a third attempt
    assert server.requests == [] and response.visualization is None
    assert steps(response) == ["plan", "validate", "plan", "validate"]


async def test_output_that_is_not_a_plan_gets_the_same_single_retry(pembro):
    llm = FakeLLM(llm_text("Here is your chart!"), llm_plan(GOOD))
    response = await pipeline_with(llm, FakeCtGov(pembro)).run(REQUEST)
    assert response.status == "ok"
    assert "not a valid plan" in response.meta.trace[1].model_dump()["errors"][0]["message"]


async def test_several_series_across_two_breakdowns_is_refused_in_plain_words(pembro):
    plan = QueryPlan(
        mode="aggregate", group_by=["phase", "sponsor_class"],
        series=[{"label": "A", "filters": {"drug": "A"}}, {"label": "B", "filters": {"drug": "B"}}],
    )
    response = await pipeline_with(FakeLLM(llm_plan(plan)), FakeCtGov(pembro)).run(QueryRequest(query="compare A and B by phase and sponsor type"))
    assert response.status == "unsupported"
    assert "not supported yet" in response.error.details[0]


# ---- unsupported questions ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("show me trials", "Name a drug, condition or sponsor to chart."),
        ("which drug works best?", "Trial records do not say how well a treatment works."),
    ],
)
async def test_question_the_model_marks_unsupported_is_returned_without_fetching(pembro, query, reason):
    server = FakeCtGov(pembro)
    plan = QueryPlan(mode="aggregate", interpretation="Unclear.", unsupported_reason=reason)
    response = await pipeline_with(FakeLLM(llm_plan(plan)), server).run(QueryRequest(query=query))
    assert response.status == "unsupported"
    assert (response.error.code, response.error.message) == ("unsupported_question", reason)
    assert server.requests == [] and steps(response) == ["plan", "validate"]
    assert response.meta.interpretation == "Unclear."


# ---- the LLM provider is down -------------------------------------------------------------------


def _rate_limited():
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.RateLimitError("quota", response=httpx.Response(429, request=request), body=None)


async def test_llm_down_is_an_error_with_the_reason(pembro):
    server = FakeCtGov(pembro)
    response = await pipeline_with(FakeLLM(_rate_limited()), server).run(REQUEST)
    assert response.status == "error" and response.error.code == "llm_unavailable"
    assert response.error.message == (
        "The question could not be interpreted: The OpenAI account is over its rate limit or out of quota."
    )
    assert server.requests == [] and steps(response) == ["plan"]
    assert response.meta.trace[0].model_dump()["error"].startswith("The OpenAI account")


async def test_llm_failing_during_the_retry_is_also_an_error(pembro):
    response = await pipeline_with(FakeLLM(llm_plan(BAD), _rate_limited()), FakeCtGov(pembro)).run(REQUEST)
    assert response.status == "error" and response.error.code == "llm_unavailable"
    assert steps(response) == ["plan", "validate", "plan"]


async def test_no_planner_configured_is_an_error_not_a_crash(pembro):
    response = await Pipeline(make_client(FakeCtGov(pembro))).run(REQUEST)
    assert response.status == "error" and response.error.code == "llm_unavailable"


async def test_a_bug_in_planning_still_returns_an_envelope(pembro):
    class Broken:
        async def draft(self, request):
            raise RuntimeError("boom")

    response = await Pipeline(make_client(FakeCtGov(pembro)), Broken()).run(REQUEST)
    assert response.status == "error" and response.error.code == "internal_error"
    assert response.meta.trace[-1].model_dump()["error"] == "RuntimeError: boom"


# ---- corrected spellings ------------------------------------------------------------------------


async def test_a_corrected_spelling_is_reported(pembro):
    plan = QueryPlan.model_validate({**GOOD.model_dump(), "search_terms": {"drug": ["pembrolizumab"]}})
    response = await pipeline_with(FakeLLM(llm_plan(plan)), FakeCtGov(pembro)).run(QueryRequest(query="pembrolizumub trials by phase"))
    assert response.meta.search_terms == {"drug": ["pembrolizumab"]}
    assert response.meta.notes[0] == "Drug searched as: pembrolizumab."


async def test_a_term_the_user_typed_themselves_is_not_reported_as_a_correction(pembro):
    plan = QueryPlan.model_validate({**GOOD.model_dump(), "search_terms": {"drug": ["Pembrolizumab", "MK-3475"]}})
    response = await pipeline_with(FakeLLM(llm_plan(plan)), FakeCtGov(pembro)).run(REQUEST)
    assert response.meta.search_terms == {"drug": ["MK-3475"]}  # only the term that was not in the question
    assert response.meta.notes[0] == "Drug searched as: MK-3475."
    assert response.meta.plan.search_terms.drug == ["MK-3475"]


async def test_a_name_given_as_a_request_field_is_not_reported_as_a_correction(pembro):
    """The example request of the assignment: the drug is in `drug_name`, not in the question."""
    plan = QueryPlan.model_validate({**GOOD.model_dump(), "group_by": ["start_year"], "search_terms": {"drug": ["pembrolizumab"]}})
    request = QueryRequest(query="How has the number of trials for this drug changed over time?", drug_name="Pembrolizumab")
    response = await pipeline_with(FakeLLM(llm_plan(plan)), FakeCtGov(pembro)).run(request)
    assert response.status == "ok" and response.visualization.type == "time_series"
    assert response.visualization.title == "Pembrolizumab trials by start year"
    assert response.meta.search_terms == {} and not any("searched as" in note for note in response.meta.notes)


async def test_no_search_terms_note_when_nothing_was_corrected(pembro):
    response = await pipeline_with(FakeLLM(llm_plan(GOOD)), FakeCtGov(pembro)).run(REQUEST)
    assert response.meta.search_terms == {} and not any("searched as" in n for n in response.meta.notes)


# ---- a lone series is folded into the plan-wide filters -----------------------------------------


def test_one_series_with_filters_becomes_plan_wide_filters():
    plan = QueryPlan(
        mode="aggregate", filters={"start_year_min": 2015}, group_by=["start_year"],
        series=[{"label": "Semaglutide", "filters": {"drug": "semaglutide"}}],
    )
    folded = canonical(plan)
    assert folded.filters.model_dump(exclude_none=True) == {"drug": "semaglutide", "start_year_min": 2015}
    assert [(s.label, s.filters.model_dump(exclude_none=True)) for s in folded.series] == [("All", {})]


def test_a_real_comparison_and_an_already_plain_plan_are_left_alone():
    two = QueryPlan(mode="aggregate", group_by=["phase"], series=[{"label": "A", "filters": {"drug": "A"}}, {"label": "B", "filters": {"drug": "B"}}])
    assert canonical(two) is two
    assert canonical(GOOD) is GOOD


async def test_folding_gives_the_chart_its_proper_title(pembro):
    plan = QueryPlan(mode="aggregate", group_by=["phase"], series=[{"label": "All", "filters": {"drug": "pembrolizumab"}}])
    response = await pipeline_with(FakeLLM(llm_plan(plan)), FakeCtGov(pembro)).run(REQUEST)
    assert response.visualization.title == "Pembrolizumab trials by phase"
    assert response.meta.filters_applied == {"All": {"drug": "pembrolizumab"}}


# ---- explicit request fields override the model's reading (spec section 5) ----------------------


def overlay(plan: QueryPlan, **request):
    new, notes = apply_request_filters(plan, QueryRequest(query="some question", **request))
    series = [(s.label, s.filters.model_dump(exclude_none=True, mode="json")) for s in new.series]
    return new.filters.model_dump(exclude_none=True, mode="json"), series, notes


def test_no_explicit_fields_changes_nothing():
    assert overlay(GOOD) == ({"drug": "pembrolizumab"}, [("All", {})], [])


def test_an_explicit_field_fills_a_filter_the_model_left_empty_without_a_note():
    filters, _, notes = overlay(GOOD, country="Germany", status=["RECRUITING"], trial_phase=["PHASE3"], sponsor="Merck")
    assert filters == {"drug": "pembrolizumab", "country": "Germany", "status": ["RECRUITING"], "phase": ["PHASE3"], "sponsor": "Merck"}
    assert notes == []


def test_an_explicit_field_replaces_a_different_value_and_says_so():
    filters, _, notes = overlay(GOOD, drug_name="nivolumab")
    assert filters == {"drug": "nivolumab"}
    assert notes == [
        "The drug_name given with the request (nivolumab) was used instead of pembrolizumab, "
        "which was read from the question."
    ]


def test_the_same_value_in_different_case_is_not_a_conflict():
    assert overlay(GOOD, drug_name="Pembrolizumab") == ({"drug": "Pembrolizumab"}, [("All", {})], [])


def test_years_map_to_the_start_year_range():
    plan = GOOD.model_copy(update={"filters": GOOD.filters.model_copy(update={"start_year_min": 2010})})
    filters, _, notes = overlay(plan, start_year=2018, end_year=2022)
    assert (filters["start_year_min"], filters["start_year_max"]) == (2018, 2022)
    assert notes == ["The start_year given with the request (2018) was used instead of 2010, which was read from the question."]


def test_status_conflict_shows_the_values_plainly():
    plan = QueryPlan(mode="aggregate", filters={"status": ["COMPLETED"]}, group_by=["phase"])
    _, _, notes = overlay(plan, status=["RECRUITING", "NOT_YET_RECRUITING"])
    assert notes == [
        "The status given with the request (RECRUITING, NOT_YET_RECRUITING) was used instead of COMPLETED, "
        "which was read from the question."
    ]


def test_several_drug_names_become_one_series_each():
    filters, series, notes = overlay(GOOD, drug_name=["nivolumab", "ipilimumab"])
    assert filters == {}
    assert series == [("nivolumab", {"drug": "nivolumab"}), ("ipilimumab", {"drug": "ipilimumab"})]
    assert notes == []


def test_several_conditions_keep_the_other_filters_plan_wide():
    filters, series, _ = overlay(GOOD, condition=["melanoma", "lung cancer"])
    assert filters == {"drug": "pembrolizumab"}
    assert series == [("melanoma", {"condition": "melanoma"}), ("lung cancer", {"condition": "lung cancer"})]


def test_explicit_names_replace_a_different_comparison_and_say_so():
    plan = QueryPlan(
        mode="aggregate", group_by=["phase"],
        series=[{"label": "Drug X", "filters": {"drug": "X"}}, {"label": "Drug Y", "filters": {"drug": "Y"}}],
    )
    _, series, notes = overlay(plan, drug_name=["A", "B", "C"])
    assert [label for label, _ in series] == ["A", "B", "C"]
    assert notes == ["The groups compared are the 3 names given in drug_name (A, B, C), not the ones read from the question."]


def test_a_single_explicit_drug_ends_a_comparison_by_drug():
    plan = QueryPlan(
        mode="aggregate", filters={"condition": "melanoma"}, group_by=["phase"],
        series=[{"label": "X", "filters": {"drug": "X"}}, {"label": "Y", "filters": {"drug": "Y"}}],
    )
    filters, series, notes = overlay(plan, drug_name="Z")
    assert filters == {"condition": "melanoma", "drug": "Z"} and series == [("All", {})]
    assert len(notes) == 1 and "was used instead of X" in notes[0]


async def test_override_reaches_the_api_and_the_response(pembro):
    server = FakeCtGov(pembro)
    request = QueryRequest(query="pembrolizumab trials by phase", drug_name="nivolumab", country="Japan")
    response = await pipeline_with(FakeLLM(llm_plan(GOOD)), server).run(request)
    params = server.page_requests()[0].url.params
    assert params["query.intr"] == "nivolumab" and 'AREA[LocationCountry]"Japan"' in params["filter.advanced"]
    assert response.visualization.title == "Nivolumab trials in Japan by phase"
    assert response.meta.filters_applied == {"All": {"drug": "nivolumab", "country": "Japan"}}
    assert response.meta.notes[0].startswith("The drug_name given with the request (nivolumab) was used instead of pembrolizumab")


async def test_overrides_are_applied_before_validation(pembro):
    """Five explicit conditions are permitted. With two group_by fields, they cause the not-supported rule."""
    plan = QueryPlan(mode="aggregate", group_by=["phase", "status"])
    request = QueryRequest(query="compare conditions", condition=["a", "b"])
    response = await pipeline_with(FakeLLM(llm_plan(plan)), FakeCtGov(pembro)).run(request)
    assert response.status == "unsupported" and "not supported yet" in response.error.details[0]


async def test_several_drugs_and_several_conditions_together_are_refused_before_the_llm_is_called(pembro):
    llm = FakeLLM(llm_plan(GOOD))
    request = QueryRequest(query="compare everything", drug_name=["a", "b"], condition=["c", "d"])
    response = await pipeline_with(llm, FakeCtGov(pembro)).run(request)
    assert response.status == "unsupported"
    assert (response.error.code, response.error.message) == ("invalid_request", BOTH_LISTS_MESSAGE)
    assert llm.requests == []
