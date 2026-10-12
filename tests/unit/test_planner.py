"""TASK: Tests the planner tool loop with a fake LLM.
GRADING: AI / Agent Design (the limit of 3 tool calls, strict output).
"""
import json

import httpx
import openai
import pytest

from app.config import Settings
from app.models import PlanFilters, QueryPlan, QueryRequest, ValidationIssue
from app.planner.planner import LIMIT_REACHED, Planner, PlannerUnavailable, strict_json_schema
from app.planner.prompt import FEW_SHOT, SYSTEM_PROMPT, user_message
from app.planner.tools import PlannerTools
from app.registry import REGISTRY
from tests.fakes import FakeCtGov, FakeLLM, llm_plan, llm_text, llm_tools, make_client

PLAN = QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab"}, group_by=["phase"], interpretation="Count by phase.")
REQUEST = QueryRequest(query="pembrolizumab trials by phase")
COUNT = ("count_trials", {"filters": {"drug": "pembrolizumab"}})


def planner_with(llm, server=None, **settings) -> Planner:
    client = make_client(server or FakeCtGov([{"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}}] * 7))
    return Planner(PlannerTools(client), llm=llm, settings=Settings(_env_file=None, **settings))


def tool_outputs(request: dict) -> list[dict]:
    return [json.loads(item["output"]) for item in request["input"] if item.get("type") == "function_call_output"]


# ---- the schema sent to the model ---------------------------------------------------------------


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _objects(item)


def test_strict_schema_closes_every_object_and_requires_every_key():
    schema = strict_json_schema(QueryPlan)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and '"default"' not in text
    objects = list(_objects(schema))
    assert len(objects) >= 6  # plan, filters, series item, its filters, bin, network, search_terms
    for node in objects:
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])


def test_strict_schema_carries_the_fixed_value_lists():
    schema = strict_json_schema(QueryPlan)
    assert schema["properties"]["mode"]["enum"] == ["aggregate", "histogram", "points", "network"]
    status = schema["properties"]["filters"]["properties"]["status"]["anyOf"][0]["items"]
    assert "RECRUITING" in status["enum"] and len(status["enum"]) == 14


def test_a_plan_matching_the_strict_schema_parses_back():
    assert QueryPlan.model_validate_json(PLAN.model_dump_json()) == PLAN
    assert set(strict_json_schema(QueryPlan)["required"]) == set(PLAN.model_dump())


# ---- the prompt ---------------------------------------------------------------------------------


def test_prompt_lists_every_registry_field_and_the_hard_rules():
    for name, field in REGISTRY.items():
        usable = field.kind.value not in ("id", "text")  # labels are not offered to the model
        assert (f"- {name} (" in SYSTEM_PROMPT) is usable
    assert "Never add a filter the user did not ask for" in SYSTEM_PROMPT
    assert "The series-versus-group_by rule" in SYSTEM_PROMPT
    assert "You never state a number" in SYSTEM_PROMPT
    # one general rule about refusing, not a list of special cases
    assert "Set unsupported_reason in exactly two cases" in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.count("never a reason") == 0
    assert "Also fill alternatives with two or three complete questions" in SYSTEM_PROMPT
    refusal = next(plan for _, plan in FEW_SHOT if plan.unsupported_reason)
    assert len(refusal.alternatives) == 3 and all(q.endswith("?") for q in refusal.alternatives)
    assert all(plan.alternatives == [] for _, plan in FEW_SHOT if not plan.unsupported_reason)


def test_prompt_has_one_example_per_mode_plus_a_comparison_and_an_unsupported_one():
    modes = [plan.mode.value for _, plan in FEW_SHOT if not plan.unsupported_reason]
    assert sorted(set(modes)) == ["aggregate", "histogram", "network", "points"]
    assert any(len(plan.series) > 1 for _, plan in FEW_SHOT)
    assert sum(bool(plan.unsupported_reason) for _, plan in FEW_SHOT) == 1
    assert 5 <= len(FEW_SHOT) <= 6
    for question, _ in FEW_SHOT:
        assert question in SYSTEM_PROMPT


def test_user_message_passes_explicit_filters_to_the_model():
    assert user_message("trials by phase", {}, 2026) == "Question: trials by phase\nCurrent year: 2026"
    assert '"drug_name": ["Keytruda"]' in user_message("trials by phase", {"drug_name": ["Keytruda"]}, 2026)


# ---- no tools needed ----------------------------------------------------------------------------


async def test_plan_without_tools_takes_one_model_call():
    llm = FakeLLM(llm_plan(PLAN))
    draft = await planner_with(llm).draft(REQUEST)
    assert draft.plan == PLAN and draft.parse_error is None
    assert (draft.llm_calls, draft.tool_calls, draft.input_tokens, draft.output_tokens) == (1, [], 100, 20)
    assert draft.model == "gpt-5.4-mini"

    [request] = llm.requests
    assert request["model"] == "gpt-5.4-mini" and request["instructions"] == SYSTEM_PROMPT
    assert request["reasoning"] == {"effort": "low"}
    assert request["tool_choice"] == "auto" and [t["name"] for t in request["tools"]] == ["list_fields", "count_trials"]
    assert request["text"]["format"]["strict"] is True and request["text"]["format"]["name"] == "QueryPlan"
    assert request["store"] is False  # nothing is kept on the provider's side
    assert request["input"][0]["content"].startswith("Question: pembrolizumab trials by phase")


async def test_explicit_request_fields_reach_the_model():
    llm = FakeLLM(llm_plan(PLAN))
    await planner_with(llm).draft(QueryRequest(query="trials by phase", drug_name="Keytruda", start_year=2020, debug=False))
    content = llm.requests[0]["input"][0]["content"]
    assert '"drug_name": ["Keytruda"]' in content and '"start_year": 2020' in content
    assert "debug" not in content and "max_citations" not in content


# ---- the tool loop ------------------------------------------------------------------------------


async def test_a_tool_call_is_run_and_its_result_sent_back():
    llm = FakeLLM(llm_tools(COUNT), llm_plan(PLAN))
    draft = await planner_with(llm).draft(REQUEST)
    assert draft.plan == PLAN and draft.llm_calls == 2
    [call] = draft.tool_calls
    assert (call.name, call.arguments, call.result) == ("count_trials", COUNT[1], {"total_count": 7})
    assert tool_outputs(llm.requests[1]) == [{"total_count": 7}]
    # the second request carries the whole conversation: question, the model's call, our answer
    assert [item.get("type", item.get("role")) for item in llm.requests[1]["input"]] == [
        "user", "function_call", "function_call_output",
    ]
    assert llm.requests[1]["input"][2]["call_id"] == "call_0"


async def test_list_fields_returns_the_registry():
    llm = FakeLLM(llm_tools(("list_fields", {})), llm_plan(PLAN))
    draft = await planner_with(llm).draft(REQUEST)
    fields = {f["name"]: f for f in draft.tool_calls[0].result["fields"]}
    assert set(fields) == set(REGISTRY)
    assert fields["status"]["values"][1] == "Recruiting" and fields["enrollment"]["unit"] == "participants"


async def test_tool_loop_stops_at_three_calls_however_often_the_model_asks():
    llm = FakeLLM(llm_tools(COUNT))  # a model that only ever asks for a tool
    draft = await planner_with(llm).draft(REQUEST)
    assert len(draft.tool_calls) == 3
    assert draft.llm_calls == 5  # bounded: 3 tool rounds, one refusal round, then stop
    assert [r["tool_choice"] for r in llm.requests] == ["auto", "auto", "auto", "none", "none"]
    assert tool_outputs(llm.requests[4])[-1] == LIMIT_REACHED  # the 4th request was refused, not run
    assert draft.plan is None and draft.parse_error == "The model returned no plan."


async def test_three_tool_calls_then_a_plan_is_the_normal_worst_case():
    llm = FakeLLM(llm_tools(COUNT), llm_tools(COUNT), llm_tools(("list_fields", {})), llm_plan(PLAN))
    draft = await planner_with(llm).draft(REQUEST)
    assert draft.plan == PLAN and len(draft.tool_calls) == 3 and draft.llm_calls == 4
    assert llm.requests[3]["tool_choice"] == "none"


async def test_parallel_tool_calls_beyond_the_limit_are_refused_not_run():
    server = FakeCtGov([])
    llm = FakeLLM(llm_tools(COUNT, COUNT, COUNT, COUNT, COUNT), llm_plan(PLAN))
    draft = await planner_with(llm, server).draft(REQUEST)
    assert len(draft.tool_calls) == 3 and draft.plan == PLAN
    outputs = tool_outputs(llm.requests[1])
    assert outputs[:3] == [{"total_count": 0}] * 3 and outputs[3:] == [LIMIT_REACHED] * 2
    assert llm.requests[1]["tool_choice"] == "none"


async def test_the_limit_is_configurable_and_zero_means_no_tools():
    llm = FakeLLM(llm_plan(PLAN))
    await planner_with(llm, planner_max_tool_calls=0).draft(REQUEST)
    assert llm.requests[0]["tool_choice"] == "none"


# ---- tools that fail still let the model plan ---------------------------------------------------


async def test_count_trials_reports_an_api_failure_to_the_model_instead_of_raising():
    server = FakeCtGov(override=lambda r: httpx.Response(500))
    llm = FakeLLM(llm_tools(COUNT), llm_plan(PLAN))
    draft = await planner_with(llm, server).draft(REQUEST)
    assert draft.plan == PLAN
    assert "ClinicalTrials.gov could not be reached" in draft.tool_calls[0].result["error"]


async def test_count_trials_rejects_filters_that_are_not_filters():
    llm = FakeLLM(llm_tools(("count_trials", {"filters": {"status": ["OPEN"]}})), llm_plan(PLAN))
    draft = await planner_with(llm).draft(REQUEST)
    assert draft.tool_calls[0].result["error"].startswith("Those filters are not valid: status.0")


async def test_an_unknown_tool_or_broken_arguments_gets_an_error_result():
    tools = PlannerTools(make_client(FakeCtGov([])))
    assert await tools.call("delete_everything", {}) == {"error": 'There is no tool named "delete_everything".'}
    assert await tools.call("count_trials", {}) == {"total_count": 0}  # no filters: counts everything


async def test_count_trials_uses_the_same_filters_as_a_plan():
    server = FakeCtGov([])
    tools = PlannerTools(make_client(server))
    await tools.call("count_trials", {"filters": PlanFilters(condition="melanoma", status=["RECRUITING"]).model_dump(mode="json")})
    params = dict(server.requests[0].url.params)
    assert params["query.cond"] == "melanoma" and params["filter.overallStatus"] == "RECRUITING" and params["pageSize"] == "1"


# ---- output that is not a plan ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "The model returned no plan."),
        ("I think you want a bar chart.", "The output was not a valid plan"),
        ('{"mode": "pie"}', "The output was not a valid plan (mode:"),
        ('{"mode": "aggregate", "filters": {"gender": "female"}}', "filters.gender"),
    ],
)
async def test_output_that_is_not_a_plan_is_recorded_not_raised(text, error):
    draft = await planner_with(FakeLLM(llm_text(text))).draft(REQUEST)
    assert draft.plan is None and error in draft.parse_error


# ---- the single retry ---------------------------------------------------------------------------


async def test_revise_sends_the_errors_back_in_the_same_conversation_without_tools():
    bad = QueryPlan(mode="aggregate", group_by=["gender"])
    llm = FakeLLM(llm_tools(COUNT), llm_plan(bad), llm_plan(PLAN))
    planner = planner_with(llm)
    first = await planner.draft(REQUEST)
    issues = [ValidationIssue(path="group_by[0]", message='"gender" is not a known field.')]
    second = await planner.revise(first, issues)

    assert first.plan == bad and second.plan == PLAN
    assert (second.llm_calls, second.input_tokens, len(second.tool_calls)) == (3, 300, 1)  # totals carry over
    retry = llm.requests[2]
    assert retry["tool_choice"] == "none"
    assert retry["input"][-1]["content"] == (
        'That plan cannot be run:\n- group_by[0]: "gender" is not a known field.\n'
        "Return a corrected plan. Change only what is needed."
    )
    assert len(retry["input"]) == 5  # question, tool call, tool result, the bad plan, the errors
    assert len(first.conversation) == 4  # the first draft is not altered by the retry


# ---- the provider fails -------------------------------------------------------------------------


def _status_error(cls, status):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (_status_error(openai.AuthenticationError, 401), "The OpenAI API key was rejected."),
        (_status_error(openai.RateLimitError, 429), "The OpenAI account is over its rate limit or out of quota."),
        (_status_error(openai.NotFoundError, 404), 'The model "gpt-5.4-mini" is not available to this API key.'),
        (openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")), "The OpenAI API could not be reached."),
        (openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com")), "The OpenAI API could not be reached."),
        (_status_error(openai.BadRequestError, 400), "The OpenAI API returned an error: boom"),
    ],
)
async def test_provider_errors_become_planner_unavailable_with_a_plain_reason(error, message):
    with pytest.raises(PlannerUnavailable) as raised:
        await planner_with(FakeLLM(error)).draft(REQUEST)
    assert str(raised.value) == message


async def test_a_provider_error_in_the_middle_of_the_tool_loop_is_also_reported():
    llm = FakeLLM(llm_tools(COUNT), _status_error(openai.RateLimitError, 429))
    with pytest.raises(PlannerUnavailable):
        await planner_with(llm).draft(REQUEST)


async def test_missing_api_key_is_reported_before_any_call(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    planner = Planner(PlannerTools(make_client(FakeCtGov([]))), settings=Settings(_env_file=None, openai_api_key=None))
    with pytest.raises(PlannerUnavailable, match="OPENAI_API_KEY is not set"):
        await planner.draft(REQUEST)
