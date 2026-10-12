"""TASK:     Asks the LLM for a query plan. Permits a maximum of 3 tool calls.
GRADING:  AI / Agent Design. The LLM only plans, and its output has a strict schema.
INPUT:    A QueryRequest.
OUTPUT:   A PlanDraft: the plan (if the output was a plan), the tool calls, and the token counts.
USED BY:  app/pipeline.py
DIAGRAM:  5 (the gates that the LLM output must pass). This file is gate 1.

PARTS:
    1. Fixed values and errors
    2. The strict output schema
    3. The Planner class: the public functions
    4. The Planner class: one LLM call and one tool call

The loop:
    ask for a plan --> does the model call a tool? --yes--> run it (maximum 3) --> ask again
                                                   --no---> read the JSON as a QueryPlan

Design notes:
    - The loop is written by hand, with no agent framework (P7). The flow is fixed.
    - Each call sends the full conversation, and the provider stores nothing (`store=False`).
      Accounts with Zero Data Retention need this. A live run found it.
    - This file does not decide if the plan is correct. app/validator.py does that.
"""
import json
import time
from datetime import date
from typing import Any

import openai
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.models import PlanDraft, PlanFilters, QueryPlan, QueryRequest, ToolCall, ValidationIssue
from app.planner.prompt import SYSTEM_PROMPT, retry_message, user_message
from app.planner.tools import PlannerTools, tool_specs

# ===== PART 1: Fixed values and errors ============================================================
# WHAT: The messages and the error type of the planner.
# WHY:  A failure of the LLM provider must give a clear message to the user.

LIMIT_REACHED = {"error": "The tool call limit is reached. Return the plan now."}
# The request fields that are filters. The LLM sees them with these names.
_EXPLICIT_FILTERS = ("drug_name", "condition", "sponsor", "trial_phase", "status", "country", "start_year", "end_year")


class PlannerUnavailable(Exception):
    """WHAT: The error when the service cannot use the LLM provider. The message is safe to show to a user."""


# ===== PART 2: The strict output schema ===========================================================
# WHAT: Makes the JSON Schema that the LLM output must obey.
# WHY:  The LLM cannot return a key or a value that does not exist.


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """WHAT:   Returns the JSON Schema of a Pydantic model in the form that OpenAI strict output needs.
    WHY:    With a strict schema, the LLM cannot return a key that does not exist.
    INPUT:  model - a Pydantic model class.
    OUTPUT: A schema where each property is mandatory and no other property is permitted.
    """
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = definitions[node["$ref"].rsplit("/", 1)[1]]
            node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        node = {k: clean(v) for k, v in node.items() if k not in ("default", "title")}
        if node.get("type") == "object" and "properties" in node:
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        return node

    return clean(schema)


def _explain(exc: openai.OpenAIError, model: str) -> str:
    """WHAT: Changes an OpenAI error into a plain message for the user."""
    if isinstance(exc, openai.AuthenticationError):
        return "The OpenAI API key was rejected."
    if isinstance(exc, openai.RateLimitError):
        return "The OpenAI account is over its rate limit or out of quota."
    if isinstance(exc, openai.NotFoundError):
        return f'The model "{model}" is not available to this API key.'
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return "The OpenAI API could not be reached."
    return f"The OpenAI API returned an error: {str(exc)[:200]}"


def _as_input(item: Any) -> dict[str, Any]:
    """WHAT: Changes one output item of a response into the form that the next request accepts."""
    return item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(vars(item))


# ===== PART 3: The Planner class: the public functions ============================================
# WHAT: `draft` makes the first plan. `revise` makes the one corrected plan.
# WHY:  Other files call only these two functions.


class Planner:
    """WHAT: Makes query plans with the LLM.
    `llm` is an `openai.AsyncOpenAI`. A test gives a fake object with the same `responses.create`.
    """

    def __init__(self, tools: PlannerTools, llm: Any = None, settings: Settings | None = None) -> None:
        """WHAT:  Prepares the output schema and the tool definitions.
        INPUT: tools - the planner tools. llm - the LLM client (optional). settings - the settings (optional).
        """
        self._tools = tools
        self._settings = settings or get_settings()
        self._llm = llm
        self._plan_format = {
            "format": {"type": "json_schema", "name": "QueryPlan", "schema": strict_json_schema(QueryPlan), "strict": True}
        }
        self._tool_specs = tool_specs(strict_json_schema(PlanFilters))


    async def draft(self, request: QueryRequest) -> PlanDraft:
        """WHAT:   Makes the first plan for a request.
        INPUT:  request - the request of the user.
        OUTPUT: A PlanDraft. Raises `PlannerUnavailable` if the LLM is not available.
        """
        explicit = request.model_dump(mode="json", include=set(_EXPLICIT_FILTERS), exclude_none=True)
        draft = PlanDraft(model=self._settings.planner_model)
        message = [{"role": "user", "content": user_message(request.query, explicit, date.today().year)}]
        response = await self._ask(draft, message, tools_allowed=self._settings.planner_max_tool_calls > 0)

        # A maximum of `planner_max_tool_calls` tools run. The loop permits one more turn, so
        # that a model that asks for one more tool gets the answer "no" and can then write a plan.
        for _ in range(self._settings.planner_max_tool_calls + 1):
            requested = [item for item in response.output if item.type == "function_call"]
            if not requested:
                break
            outputs = [await self._run_tool(draft, call) for call in requested]
            remaining = self._settings.planner_max_tool_calls - len(draft.tool_calls)
            response = await self._ask(draft, outputs, tools_allowed=remaining > 0)
        return self._finish(draft, response)

    async def revise(self, draft: PlanDraft, issues: list[ValidationIssue]) -> PlanDraft:
        """WHAT:   Makes the one corrected plan after a validation failure.
        WHY:    The LLM can correct an error when it gets the exact problem.
        INPUT:  draft - the first draft. issues - the validation problems.
        OUTPUT: A new PlanDraft. Tools are not permitted in this call.
        """
        retry = draft.model_copy(update={"plan": None, "parse_error": None, "conversation": list(draft.conversation)})
        problems = [f"{issue.path}: {issue.message}" if issue.path else issue.message for issue in issues]
        message = [{"role": "user", "content": retry_message(problems)}]
        return self._finish(retry, await self._ask(retry, message, tools_allowed=False))


    # ===== PART 4: The Planner class: one LLM call and one tool call ==============================
    # WHAT: The steps that `draft` and `revise` use.
    # WHY:  Each step is small, so that a test can examine it.

    def _client(self) -> Any:
        """WHAT: Returns the LLM client. Makes it on the first call. Raises `PlannerUnavailable` if there is no key."""
        if self._llm is None:
            key = self._settings.openai_api_key
            if key is None or not key.get_secret_value():
                raise PlannerUnavailable("OPENAI_API_KEY is not set.")
            self._llm = openai.AsyncOpenAI(api_key=key.get_secret_value(), timeout=self._settings.llm_timeout_seconds)
        return self._llm

    async def _ask(self, draft: PlanDraft, items: list[dict[str, Any]], tools_allowed: bool) -> Any:
        """WHAT:   Does one LLM call.
        INPUT:  draft - the draft with the conversation. items - the new items to send.
                tools_allowed - True if the LLM can call a tool in this turn.
        OUTPUT: The response of the LLM. The conversation and the token counts are updated.
        """
        llm = self._client()
        draft.conversation += items
        try:
            response = await llm.responses.create(
                model=self._settings.planner_model,
                instructions=SYSTEM_PROMPT,
                input=draft.conversation,
                tools=self._tool_specs,
                tool_choice="auto" if tools_allowed else "none",
                text=self._plan_format,
                reasoning={"effort": self._settings.planner_reasoning_effort},
                store=False,
                # The provider stores nothing, so the reasoning of the model must go with its tool calls.
                include=["reasoning.encrypted_content"],
            )
        except openai.OpenAIError as exc:
            raise PlannerUnavailable(_explain(exc, self._settings.planner_model)) from exc
        draft.llm_calls += 1
        draft.conversation += [_as_input(item) for item in response.output]
        if response.usage is not None:
            draft.input_tokens += response.usage.input_tokens
            draft.output_tokens += response.usage.output_tokens
        return response

    async def _run_tool(self, draft: PlanDraft, call: Any) -> dict[str, Any]:
        """WHAT:   Runs one tool call that the LLM asked for, if the limit permits.
        INPUT:  draft - the draft. call - the tool call from the LLM.
        OUTPUT: The tool result in the form that the next LLM call accepts.
        """
        if len(draft.tool_calls) >= self._settings.planner_max_tool_calls:
            result: Any = LIMIT_REACHED
        else:
            try:
                arguments = json.loads(call.arguments or "{}")
            except ValueError:
                arguments = {}
            started = time.perf_counter()
            result = await self._tools.call(call.name, arguments if isinstance(arguments, dict) else {})
            draft.tool_calls.append(
                ToolCall(name=call.name, arguments=arguments, result=result, ms=round((time.perf_counter() - started) * 1000, 1))
            )
        return {"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result)}

    @staticmethod
    def _finish(draft: PlanDraft, response: Any) -> PlanDraft:
        """WHAT:   Reads the last text of the LLM as a plan.
        WHY:    Text that is not a plan gets the same handling as a plan that is not valid:
                one retry, then an `unsupported` response.
        INPUT:  draft - the draft. response - the last response of the LLM.
        OUTPUT: The draft, with `plan` set or with `parse_error` set.
        """
        text = response.output_text or ""
        try:
            draft.plan = QueryPlan.model_validate_json(text)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(map(str, first["loc"]))
            draft.parse_error = f"The output was not a valid plan ({where}: {first['msg']})." if text else (
                "The model returned no plan."
            )
        return draft
