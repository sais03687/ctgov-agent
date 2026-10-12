"""TASK:     Holds the instructions and the examples for the planner LLM.
GRADING:  AI / Agent Design. The rules are general, and the field list comes from code.
INPUT:    The field registry (app/registry.py).
OUTPUT:   SYSTEM_PROMPT, and the functions that make the user message and the retry message.
USED BY:  app/planner/planner.py

PARTS:
    1. The field table for the prompt
    2. The examples
    3. The system prompt
    4. The messages for each request

The prompt cannot become different from the code:
    - The field table comes from the field registry.
    - Each example is a real QueryPlan object. A test sends each example through the validator.
Each rule in the prompt is a general principle. No rule is a patch for one question.
"""
import json

from app.models import FieldKind, OverallStatus, Phase, QueryPlan
from app.registry import REGISTRY


# ===== PART 1: The field table for the prompt =====================================================
# WHAT: Makes the list of fields that the LLM sees.
# WHY:  The list comes from code, so the LLM sees only fields that exist.


def _field_table() -> str:
    """WHAT: Returns the field table as text. Each row has the name, the kind, and the permitted values of a field."""
    lines = []
    for field in REGISTRY.values():
        if field.kind in (FieldKind.ID, FieldKind.TEXT):
            continue  # Labels only. A plan cannot group, bin, or plot by these fields.
        values = f" Values: {', '.join(field.labels.values())}." if field.labels else ""
        unit = f" Unit: {field.unit}." if field.unit else ""
        lines.append(f"- {field.name} ({field.kind.value}): {field.display_name}.{unit}{values}")
    return "\n".join(lines)


# ===== PART 2: The examples =======================================================================
# WHAT: Example questions with their correct plans.
# WHY:  Examples show the LLM the form of a good plan.

# (question, plan) pairs: one for each mode, plus a comparison, plus a question that must be refused.
FEW_SHOT: list[tuple[str, QueryPlan]] = [
    (
        "Which countries have the most recruiting trials for lung cancer?",
        QueryPlan(
            mode="aggregate",
            filters={"condition": "lung cancer", "status": ["RECRUITING"]},
            group_by=["country"],
            sort="desc",
            limit=10,
            interpretation="Count recruiting lung cancer trials per country and show the ten countries with the most.",
        ),
    ),
    (
        "Compare how many trials started each year for pembrolizumab vs nivolumab since 2018",
        QueryPlan(
            mode="aggregate",
            filters={"start_year_min": 2018},
            series=[
                {"label": "Pembrolizumab", "filters": {"drug": "pembrolizumab"}},
                {"label": "Nivolumab", "filters": {"drug": "nivolumab"}},
            ],
            group_by=["start_year"],
            sort="natural",
            limit=50,
            interpretation="Count trials by start year from 2018 on, separately for pembrolizumab and nivolumab.",
        ),
    ),
    (
        "What does the distribution of enrollment sizes look like for breast cancer trials?",
        QueryPlan(
            mode="histogram",
            filters={"condition": "breast cancer"},
            bin={"field": "enrollment", "bin_width": 100},
            sort="natural",
            limit=50,
            interpretation="Show how many breast cancer trials fall into each enrollment-size range.",
        ),
    ),
    (
        "Do bigger phase 3 Alzheimer's trials take longer?",
        QueryPlan(
            mode="points",
            filters={"condition": "Alzheimer's disease", "phase": ["PHASE3"]},
            x_field="enrollment",
            y_field="duration_months",
            sort="natural",
            limit=50,
            interpretation="Plot each Phase 3 Alzheimer's disease trial by its enrollment and its duration in months.",
        ),
    ),
    (
        "Show a network of sponsors and the drugs they test in glioblastoma trials",
        QueryPlan(
            mode="network",
            filters={"condition": "glioblastoma"},
            network={"source_field": "sponsor", "target_field": "intervention", "max_edges": 60},
            sort="desc",
            limit=50,
            interpretation="Link each sponsor to the interventions used in its glioblastoma trials.",
        ),
    ),
    (
        "Which drug works best for lung cancer?",
        QueryPlan(
            mode="aggregate",
            interpretation="The user asks which lung cancer drug is most effective.",
            unsupported_reason=(
                "Trial registry records say how trials are set up (phase, status, sponsor, dates, size, "
                "locations), not how well a treatment works."
            ),
            alternatives=[
                "Which drugs are tested most often in lung cancer trials?",
                "How are lung cancer trials distributed across phases?",
                "How many completed lung cancer trials have posted results?",
            ],
        ),
    ),
]


def _examples() -> str:
    """WHAT: Returns the examples as text: each question with its plan as JSON."""
    blocks = []
    for question, plan in FEW_SHOT:
        blocks.append(f"Question: {question}\nPlan: {json.dumps(plan.model_dump(mode='json'), ensure_ascii=False)}")
    return "\n\n".join(blocks)


# ===== PART 3: The system prompt ==================================================================
# WHAT: The instructions that go with each LLM call.
# WHY:  The rules tell the LLM to plan only, and when to refuse.

SYSTEM_PROMPT = f"""You turn a question about clinical trials into a query plan, as JSON.

You do not answer the question. You never state a number, a count, a trial id or a conclusion:
code fetches the trials from ClinicalTrials.gov and does all counting. Your only output is the plan.

## The plan

mode - how trials become data points:
- aggregate: count trials per bucket of 1 or 2 fields (group_by). Trends per year, distributions,
  rankings and comparisons are all aggregate.
- histogram: spread of one numeric field. Set bin = {{"field": ..., "bin_width": ...}} with a round width.
- points: one mark per trial. Either x_field and y_field (two numeric fields, a scatter plot), or
  start_field "start_date" and end_field "completion_date" (a timeline bar per trial).
- network: entities as nodes, linked when they appear in the same trial. Set network.source_field and
  network.target_field. The same field twice means co-occurrence (drugs used together).

filters - apply to every series. Text filters: condition, drug, sponsor, country.
  status: any of {", ".join(s.value for s in OverallStatus)}.
  phase: any of {", ".join(p.value for p in Phase)}.
  start_year_min, start_year_max: whole years.

series - groups THE USER NAMES to compare ("drug A vs drug B", "melanoma and lung cancer"). Each
  series has a label and its own filters. If nothing is compared, use exactly one series labelled
  "All" whose filter values are all null, and put the drug, condition and other filters in the
  plan-wide filters instead.

group_by - buckets that COME FROM THE DATA (phases, years, countries, sponsors).

The series-versus-group_by rule: if the user lists the things to compare, they are series. If the
user asks for a breakdown "by" or "per" or "across" something, that is group_by. Never put data
buckets in series, and never put named comparison groups in group_by.

## Fields you may use in group_by, bin, x_field, y_field, start_field, end_field, network

{_field_table()}

Use these names exactly. There are no other fields.

## Rules

1. Never add a filter the user did not ask for. No default status, no default date range, no
   default phase. "Recent" without a year is not a filter.
2. Put drug, condition and sponsor names in filters as the user means them, with spelling fixed.
   ClinicalTrials.gov already matches brand names and synonyms, so do not expand them yourself.
   Whether a name exists is for the registry to say, not for you: a name you do not recognise is a
   subject like any other and is searched as written.
   Names are matched as whole words against what the registry holds. When you know that the
   registry records a subject under a different name than the everyday one the user typed (an
   organisation's registered legal name, for instance), use the registered name and list it in
   search_terms; when unsure which one finds the trials, compare them with count_trials.
3. Tools are optional and limited to 3 calls. Use count_trials to check that a name finds trials
   when it looks misspelled or unfamiliar. If the term you settle on differs from what the user
   typed, put it in filters and also list it in search_terms.
4. sort: "natural" keeps the field's own order (years, phases and dates run earliest first);
   "desc" for "most", "top", "common", "latest"; "asc" for "least", "fewest", "earliest" by size.
   limit: the number the user asks for, otherwise 10; use 50 where order is natural.
5. chart_type: null unless the user names a chart type; if they do, pass their choice through.
   It is only a suggestion; code decides what is drawn.
6. Your job is to describe the data that was asked for, not to judge how the result will turn
   out. Code checks every plan against the real data and explains to the user whatever does not
   work out: no matching trials, too many trials, a chart type that does not fit the data, missing
   values. So write a plan whenever the question names something to count or plot.
   Set unsupported_reason in exactly two cases:
   (a) there is nothing to plan: the question names no drug, condition, sponsor or other subject;
   (b) the answer is not in trial records at all: which treatment works best, safety, cost,
       predictions, medical advice, or a property the fields above do not cover.
   Then write one or two plain sentences saying why, and still fill the other keys with harmless
   defaults. Also fill alternatives with two or three complete questions that this system can
   answer and that stay as close as possible to what the user seems to want: keep their drug,
   condition or sponsor, and replace only the part that cannot be answered with something trial
   records do hold (phases, statuses, years, countries, sponsors, drugs, sizes, durations).
   Each alternative is sent back to you word for word when the user clicks it, so it must be a
   question you would plan, not refuse: it names one specific real drug, condition or sponsor
   (never a placeholder such as "a specific drug"), and asks for a count or a plot of fields
   listed above. If the user named no subject, pick well-known ones.
   When the question can be planned, alternatives is [].
7. interpretation: one plain sentence restating what will be counted or plotted.
8. Every key must be present. Keys that belong to another mode are null (group_by is []).

## Examples

{_examples()}
"""


# ===== PART 4: The messages for each request ======================================================
# WHAT: The text that changes for each request.
# WHY:  The LLM needs the question, the explicit filters, and the current year.


def user_message(query: str, explicit_filters: dict, current_year: int) -> str:
    """WHAT:   Returns the part of the prompt that is different for each request.
    INPUT:  query - the question. explicit_filters - the request fields that the user set.
            current_year - the current year.
    OUTPUT: The message text.
    """
    parts = [f"Question: {query}", f"Current year: {current_year}"]
    if explicit_filters:
        parts.append(
            "The user also set these filters explicitly. Code applies them whatever you write; "
            f"keep your plan consistent with them: {json.dumps(explicit_filters, ensure_ascii=False)}"
        )
    return "\n".join(parts)


def retry_message(issues: list[str]) -> str:
    """WHAT:   Returns the message that is sent one time when a plan fails validation.
    INPUT:  issues - the validation problems as text.
    OUTPUT: The message text.
    """
    listed = "\n".join(f"- {issue}" for issue in issues)
    return f"That plan cannot be run:\n{listed}\nReturn a corrected plan. Change only what is needed."
