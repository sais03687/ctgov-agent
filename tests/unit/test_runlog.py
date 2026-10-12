"""TASK: Tests the text log of the last question.
GRADING: Code Quality.
"""
import pytest

from app.config import Settings
from app.models import QueryPlan, QueryRequest
from app.pipeline import Pipeline
from app.planner.planner import Planner
from app.planner.tools import PlannerTools
from app.runlog import RunLog
from tests.fakes import FakeCtGov, FakeLLM, llm_plan, llm_tools, make_client

GOOD = QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab"}, group_by=["phase"], sort="natural", interpretation="Count by phase.")
REFUSED = QueryPlan(mode="aggregate", unsupported_reason="Trial records do not say which drug works best.", alternatives=["Which drugs are studied most often in melanoma trials?"])


@pytest.fixture
def pembro(load_fixture):
    return load_fixture("pembrolizumab_page")["studies"]


def pipeline_with(llm, server, path) -> Pipeline:
    client = make_client(server)
    return Pipeline(client, Planner(PlannerTools(client), llm=llm, settings=Settings(_env_file=None)), run_log=RunLog(path))


async def test_the_log_tells_the_whole_run_in_order(pembro, tmp_path):
    path = tmp_path / "logs" / "last_run.txt"  # the folder does not exist yet
    llm = FakeLLM(llm_tools(("count_trials", {"filters": {"drug": "pembrolizumab"}})), llm_plan(GOOD))
    request = QueryRequest(query="pembrolizumab trials by phase", country="United States")
    response = await pipeline_with(llm, FakeCtGov(pembro), path).run(request)
    text = path.read_text(encoding="utf-8")
    assert response.status == "ok"
    order = [text.index(part) for part in (
        "question: pembrolizumab trials by phase", "country: United States", "1. PLAN", "count_trials", "2. VALIDATE",
        "3. FETCH", "4. AGGREGATE", "5. CITE", "6. BUILD", "THE PLAN THAT WAS RUN",
        "SEARCHES SENT TO CLINICALTRIALS.GOV", "status: ok", "title: Pembrolizumab trials in United States by phase",
    )]
    assert order == sorted(order)
    assert "api calls: " in text and "input tokens: 200" in text
    assert '"country": "United States"' in text.split("THE PLAN THAT WAS RUN")[1]


async def test_the_next_question_replaces_the_log(pembro, tmp_path):
    path = tmp_path / "last_run.txt"
    pipeline = pipeline_with(FakeLLM(llm_plan(GOOD), llm_plan(REFUSED)), FakeCtGov(pembro), path)
    await pipeline.run(QueryRequest(query="pembrolizumab trials by phase"))
    await pipeline.run(QueryRequest(query="which drug works best for melanoma?"))
    text = path.read_text(encoding="utf-8")
    assert "pembrolizumab trials by phase" not in text and "FETCH" not in text
    assert "question: which drug works best for melanoma?" in text
    assert "status: unsupported" in text and "reason code: unsupported_question" in text
    assert "offered instead: Which drugs are studied most often in melanoma trials?" in text


async def test_the_log_is_emptied_as_soon_as_a_question_arrives(tmp_path):
    path = tmp_path / "last_run.txt"
    path.write_text("everything about the previous question", encoding="utf-8")
    RunLog(path).start(QueryRequest(query="lupus trials by phase"))
    text = path.read_text(encoding="utf-8")
    assert "previous question" not in text and "question: lupus trials by phase" in text and "Working on it" in text


async def test_a_log_that_cannot_be_written_does_not_fail_the_answer(pembro, tmp_path):
    blocker = tmp_path / "a_file_not_a_folder"
    blocker.write_text("x", encoding="utf-8")
    response = await pipeline_with(FakeLLM(llm_plan(GOOD)), FakeCtGov(pembro), blocker / "last_run.txt").run(
        QueryRequest(query="pembrolizumab trials by phase")
    )
    assert response.status == "ok"


async def test_no_log_is_written_unless_one_is_configured(pembro, tmp_path):
    client = make_client(FakeCtGov(pembro))
    pipeline = Pipeline(client, Planner(PlannerTools(client), llm=FakeLLM(llm_plan(GOOD)), settings=Settings(_env_file=None)))
    assert (await pipeline.run(QueryRequest(query="pembrolizumab trials by phase"))).status == "ok"
    assert list(tmp_path.iterdir()) == []


async def test_the_log_shows_what_each_model_call_returned_word_for_word(pembro, tmp_path):
    """The exact text of the model, before code changed it: the tool request, then the plan."""
    path = tmp_path / "last_run.txt"
    llm = FakeLLM(llm_tools(("count_trials", {"filters": {"drug": "pembrolizumab"}})), llm_plan(GOOD))
    await pipeline_with(llm, FakeCtGov(pembro), path).run(QueryRequest(query="pembrolizumab trials by phase", country="United States"))
    text = path.read_text(encoding="utf-8")
    raw = text[text.index("RAW LANGUAGE MODEL INPUT AND OUTPUT"):text.index("THE PLAN THAT WAS RUN")]
    order = [raw.index(part) for part in (
        ">> SENT TO THE MODEL (message)", "pembrolizumab trials by phase", "asks to run the tool `count_trials`",
        ">> SENT TO THE MODEL (the tool's result)", '"total_count": 50', "<< MODEL RETURNED (output text, word for word)",
        '"interpretation": "Count by phase."', "== HOW THE SYSTEM USED IT: parsed into the plan",
    )]
    assert order == sorted(order)
    # the raw plan is the model's; the country came from the request and is only in the plan that was run
    assert "United States" not in raw.split("<< MODEL RETURNED (output text, word for word)")[1]


async def test_output_that_is_not_a_plan_is_logged_as_it_came(pembro, tmp_path):
    from tests.fakes import llm_text

    path = tmp_path / "last_run.txt"
    response = await pipeline_with(FakeLLM(llm_text("Sure! Here is a chart."), llm_text("{broken")), FakeCtGov(pembro), path).run(
        QueryRequest(query="pembrolizumab trials by phase")
    )
    text = path.read_text(encoding="utf-8")
    assert response.status == "unsupported"
    assert "Sure! Here is a chart." in text and "{broken" in text and "== HOW THE SYSTEM USED IT: not usable" in text
