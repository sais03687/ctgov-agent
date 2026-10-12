"""TASK:     Gives the planner two read-only tools: list_fields and count_trials.
GRADING:  AI / Agent Design. The LLM can make sure that a name finds trials before it plans.
INPUT:    A tool name and its arguments, from the LLM.
OUTPUT:   The tool result as a dict. A failure is a dict with an "error" key.
USED BY:  app/planner/planner.py

PARTS:
    1. The tool definitions
    2. The tool functions

    list_fields()           Gives the field names, kinds, and permitted values.
    count_trials(filters)   Gives the number of trials for a set of filters. One API call.

The two tools only read. They cannot change a plan, and they cannot get trial records.
"""
from typing import Any

from pydantic import ValidationError

from app.ctgov.client import CtGovClient, CtGovError
from app.models import PlanFilters
from app.registry import REGISTRY

# ===== PART 1: The tool definitions ===============================================================
# WHAT: The names and the argument schemas that the LLM sees.
# WHY:  The LLM can call only these two tools.

LIST_FIELDS = "list_fields"
COUNT_TRIALS = "count_trials"


def tool_specs(filters_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """WHAT:   Returns the tool definitions in the Responses API format.
    INPUT:  filters_schema - the strict JSON Schema of PlanFilters.
    OUTPUT: A list of two tool definitions. `count_trials` accepts exactly the same filters as a plan.
    """
    return [
        {
            "type": "function",
            "name": LIST_FIELDS,
            "description": "List every field a plan may use, with its kind and allowed values.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "strict": True,
        },
        {
            "type": "function",
            "name": COUNT_TRIALS,
            "description": (
                "Count the trials on ClinicalTrials.gov that match the given filters. Use it to check "
                "that a drug, condition or sponsor name finds trials before putting it in the plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {"filters": filters_schema},
                "required": ["filters"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]


# ===== PART 2: The tool functions =================================================================
# WHAT: The code that runs when the LLM calls a tool.
# WHY:  A tool gives the LLM a real fact, so that it does not guess.


class PlannerTools:
    """WHAT: Runs the tools of the planner."""

    def __init__(self, client: CtGovClient) -> None:
        """WHAT: Keeps the registry client that `count_trials` uses."""
        self._client = client

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """WHAT:   Runs one tool. Does not raise an exception.
        WHY:    The LLM can make a plan without the answer of the tool.
        INPUT:  name - the tool name. arguments - the arguments from the LLM.
        OUTPUT: The tool result, or `{"error": ...}` on a failure.
        """
        if name == LIST_FIELDS:
            return self.list_fields()
        if name == COUNT_TRIALS:
            return await self.count_trials(arguments.get("filters") or {})
        return {"error": f'There is no tool named "{name}".'}

    @staticmethod
    def list_fields() -> dict[str, Any]:
        """WHAT: Returns each field with its kind and its permitted values."""
        return {
            "fields": [
                {
                    "name": field.name,
                    "kind": field.kind.value,
                    "meaning": field.display_name,
                    **({"values": list(field.labels.values())} if field.labels else {}),
                    **({"unit": field.unit} if field.unit else {}),
                }
                for field in REGISTRY.values()
            ]
        }

    async def count_trials(self, filters: dict[str, Any]) -> dict[str, Any]:
        """WHAT:   Returns the exact number of trials for a set of filters.
        INPUT:  filters - the filters, in the same form as in a plan.
        OUTPUT: `{"total_count": n}`.
        """
        try:
            parsed = PlanFilters.model_validate(filters)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            return {"error": f"Those filters are not valid: {problems}"}
        try:
            return {"total_count": await self._client.count(parsed)}
        except CtGovError as exc:
            return {"error": f"ClinicalTrials.gov could not be reached ({exc}). Plan without the count."}
