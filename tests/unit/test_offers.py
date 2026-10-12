"""TASK: Tests the suggestions that the user gets when there is no chart.
GRADING: Code Quality (robustness). Each suggestion must work.
"""
import httpx

from app.config import Settings
from app.models import QueryPlan, QueryRequest
from app.pipeline import STARTER_QUESTIONS, Pipeline
from app.planner.planner import Planner
from app.planner.tools import PlannerTools
from tests.fakes import FakeCtGov, FakeLLM, llm_plan, make_client

REFUSED = QueryPlan(
    mode="aggregate",
    unsupported_reason="Trial records do not say which drug works best.",
    alternatives=["Which drugs are studied most often in melanoma trials?", "Count trials for a specific drug.", "How are melanoma trials distributed across phases?"],
)
BY_DRUG = QueryPlan(mode="aggregate", filters={"condition": "melanoma"}, group_by=["intervention"])
BY_PHASE = QueryPlan(mode="aggregate", filters={"condition": "melanoma"}, group_by=["phase"])
NARROW = QueryPlan(mode="aggregate", filters={"drug": "nivolumab", "country": "Iceland"}, group_by=["phase"])


def counting_server(count_for) -> FakeCtGov:
    """Returns a registry whose count depends on the search. `count_for(params)` gives the count."""
    def serve(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/studies"):
            return httpx.Response(200, json={"totalCount": count_for(request.url.params), "studies": []})
        return None

    return FakeCtGov(override=serve)


def pipeline_with(llm, server, check: bool) -> Pipeline:
    client = make_client(server)
    planner = Planner(PlannerTools(client), llm=llm, settings=Settings(_env_file=None))
    return Pipeline(client, planner, check_suggestions=check)


async def test_an_offered_question_that_is_refused_is_not_offered():
    """The model gives three alternatives. The second one is refused when it is planned.

    Thus only the other two are shown, each with its trial count.
    """
    llm = FakeLLM(llm_plan(REFUSED), llm_plan(BY_DRUG), llm_plan(REFUSED), llm_plan(BY_PHASE), llm_plan(REFUSED))
    response = await pipeline_with(llm, counting_server(lambda p: 3775), check=True).run(
        QueryRequest(query="which drug works best for melanoma?")
    )
    offers = response.error.suggestions
    assert response.status == "unsupported"
    assert [o.query for o in offers] == [REFUSED.alternatives[0], REFUSED.alternatives[2]]
    assert [o.trials_matched for o in offers] == [3775, 3775]


async def test_an_offered_question_with_no_trials_or_too_many_trials_is_not_offered():
    for count in (0, 50_000):  # an explode field has no exact-count path, so 50,000 does not fit
        llm = FakeLLM(llm_plan(REFUSED), llm_plan(BY_DRUG))  # each alternative plans as "melanoma by drug"
        response = await pipeline_with(llm, counting_server(lambda p, n=count: n), check=True).run(
            QueryRequest(query="which drug works best for melanoma?")
        )
        assert [o.query for o in response.error.suggestions] == list(STARTER_QUESTIONS)


async def test_no_trials_offers_the_question_without_one_filter():
    """Nivolumab in Iceland finds no trials. Each filter is removed in turn and the result is counted."""
    def count_for(params) -> int:
        drug, country = "query.intr" in params, "Iceland" in params.get("filter.advanced", "")
        return 0 if drug and country else 137 if drug else 19

    response = await pipeline_with(FakeLLM(llm_plan(NARROW)), counting_server(count_for), check=False).run(
        QueryRequest(query="nivolumab trials in Iceland by phase")
    )
    assert response.status == "ok" and response.meta.trials_matched == 0
    assert [(o.query, o.trials_matched) for o in response.meta.suggestions] == [
        ("Trials in Iceland by phase", 19), ("Nivolumab trials by phase", 137),
    ]
    assert response.meta.suggestions[0].label == "Trials in Iceland by phase (without the drug)"
    assert response.meta.trace[-1].step == "suggest"


async def test_no_trials_with_one_filter_offers_the_starter_questions():
    plan = QueryPlan(mode="aggregate", filters={"drug": "xyzzyqwerty"}, group_by=["phase"])
    response = await pipeline_with(FakeLLM(llm_plan(plan)), counting_server(lambda p: 0), check=False).run(
        QueryRequest(query="xyzzyqwerty trials by phase")
    )
    assert [o.query for o in response.meta.suggestions] == list(STARTER_QUESTIONS)


async def test_an_answer_with_trials_has_no_offers(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    plan = QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab"}, group_by=["phase"])
    response = await pipeline_with(FakeLLM(llm_plan(plan)), FakeCtGov(studies), check=False).run(
        QueryRequest(query="pembrolizumab trials by phase")
    )
    assert response.meta.trials_matched == 50 and response.meta.suggestions == []
