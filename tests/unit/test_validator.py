"""TASK: Tests each rule of the plan validator.
GRADING: AI / Agent Design (validation of LLM output).
"""
from datetime import date

import pytest

from app.models import QueryPlan
from app.planner.prompt import FEW_SHOT
from app.validator import validate

TWO_DRUGS = [{"label": "A", "filters": {"drug": "A"}}, {"label": "B", "filters": {"drug": "B"}}]


def issues_of(**plan) -> dict[str, str]:
    """Returns path -> message for each problem of a plan given as keyword arguments."""
    return {issue.path: issue.message for issue in validate(QueryPlan(**plan))}


# ---- plans that must pass -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan",
    [
        {"mode": "aggregate", "group_by": ["phase"]},
        {"mode": "aggregate", "group_by": ["start_year", "phase"], "filters": {"condition": "asthma"}},
        {"mode": "aggregate", "group_by": ["has_results"]},
        {"mode": "aggregate", "group_by": ["enrollment"], "bin": {"field": "enrollment", "bin_width": 100}},
        {"mode": "aggregate", "group_by": ["phase"], "series": TWO_DRUGS, "limit": 50},
        {"mode": "aggregate", "group_by": ["phase"], "filters": {"start_year_min": 2015, "start_year_max": 2015}},
        {"mode": "histogram", "bin": {"field": "duration_months", "bin_width": 6}},
        {"mode": "points", "x_field": "enrollment", "y_field": "duration_months"},
        {"mode": "points", "start_field": "start_date", "end_field": "completion_date"},
        {"mode": "network", "network": {"source_field": "sponsor", "target_field": "intervention"}},
        {"mode": "network", "network": {"source_field": "intervention", "target_field": "intervention"}},
    ],
)
def test_valid_plans_have_no_issues(plan):
    assert validate(QueryPlan(**plan)) == []


@pytest.mark.parametrize("question, plan", FEW_SHOT, ids=[q[:40] for q, _ in FEW_SHOT])
def test_every_example_in_the_prompt_is_a_valid_plan(question, plan):
    assert validate(plan) == []


def test_a_plan_marked_unsupported_is_not_checked_further():
    plan = QueryPlan(mode="aggregate", group_by=["nonsense"], limit=999, unsupported_reason="Too vague.")
    assert validate(plan) == []


# ---- deliberately bad plans ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "path", "says"),
    [
        # 1. a field that does not exist
        ({"mode": "aggregate", "group_by": ["gender"]}, "group_by[0]", '"gender" is not a known field'),
        # 2. a field of the wrong kind for the job
        ({"mode": "aggregate", "group_by": ["nct_id"]}, "group_by[0]", '"nct_id" cannot be used in group_by'),
        ({"mode": "aggregate", "group_by": ["completion_date"]}, "group_by[0]", "cannot be used in group_by"),
        # 3. grouping by a number without bins
        ({"mode": "aggregate", "group_by": ["enrollment"]}, "bin", '"enrollment" is a number, so grouping by it needs bin'),
        # 4. too few and too many group_by fields
        ({"mode": "aggregate"}, "group_by", "needs 1 or 2 group_by fields, not 0"),
        ({"mode": "aggregate", "group_by": ["phase", "status", "country"]}, "group_by", "not 3"),
        ({"mode": "aggregate", "group_by": ["phase", "phase"]}, "group_by", "same field twice"),
        # 5. limit out of range
        ({"mode": "aggregate", "group_by": ["phase"], "limit": 0}, "limit", "between 1 and 50, not 0"),
        ({"mode": "aggregate", "group_by": ["phase"], "limit": 500}, "limit", "between 1 and 50, not 500"),
        # 6. keys of another mode
        (
            {"mode": "aggregate", "group_by": ["phase"], "network": {"source_field": "sponsor", "target_field": "intervention"}},
            "network", "network belongs to network mode; leave it empty in aggregate mode",
        ),
        ({"mode": "aggregate", "group_by": ["phase"], "x_field": "enrollment"}, "x_field", "belongs to points mode"),
        ({"mode": "histogram", "bin": {"field": "enrollment", "bin_width": 50}, "group_by": ["phase"]}, "group_by", "belongs to aggregate mode"),
        # 7. mode-required keys missing
        ({"mode": "histogram"}, "bin", "histogram mode needs bin with a numeric field"),
        ({"mode": "network"}, "network", "network mode needs network"),
        ({"mode": "points"}, "x_field", "points mode needs either x_field and y_field"),
        ({"mode": "points", "x_field": "enrollment"}, "y_field", "y_field is missing"),
        # 8. x / y must be numeric, bins must be numeric and positive
        ({"mode": "points", "x_field": "phase", "y_field": "enrollment"}, "x_field", '"phase" cannot be used as a numeric axis'),
        ({"mode": "points", "x_field": "enrollment", "y_field": "enrollment"}, "y_field", "the same field"),
        ({"mode": "histogram", "bin": {"field": "phase", "bin_width": 10}}, "bin.field", '"phase" cannot be used for bins'),
        ({"mode": "histogram", "bin": {"field": "enrollment", "bin_width": 0}}, "bin.bin_width", "greater than 0"),
        ({"mode": "points", "start_field": "start_year", "end_field": "completion_date"}, "start_field", "Fields that can: start_date, completion_date, last_updated"),
        ({"mode": "points", "start_field": "start_date"}, "end_field", "end_field is missing"),
        (
            {"mode": "points", "start_field": "start_date", "end_field": "completion_date", "series": TWO_DRUGS},
            "series", "A timeline of trials shows one series",
        ),
        ({"mode": "histogram", "bin": {"field": "enrollment", "bin_width": 50}, "series": TWO_DRUGS}, "series", "A histogram shows one series"),
        # 9. network fields must be categorical, edges bounded
        ({"mode": "network", "network": {"source_field": "enrollment", "target_field": "sponsor"}}, "network.source_field", "cannot be used as network nodes"),
        ({"mode": "network", "network": {"source_field": "sponsor", "target_field": "drug"}}, "network.target_field", '"drug" is not a known field'),
        ({"mode": "network", "network": {"source_field": "sponsor", "target_field": "intervention", "max_edges": 5000}}, "network.max_edges", "between 1 and 200"),
        # 10. series count and content
        ({"mode": "aggregate", "group_by": ["phase"], "series": []}, "series", "1 to 5 series, not 0"),
        (
            {"mode": "aggregate", "group_by": ["phase"], "series": [{"label": str(i), "filters": {"drug": str(i)}} for i in range(6)]},
            "series", "1 to 5 series, not 6",
        ),
        (
            {"mode": "aggregate", "group_by": ["phase"], "series": [{"label": "A", "filters": {"drug": "A"}}, {"label": "a", "filters": {"drug": "B"}}]},
            "series", "same label",
        ),
        (
            {"mode": "aggregate", "group_by": ["phase"], "series": [{"label": "A", "filters": {"drug": "A"}}, {"label": "B"}]},
            "series", "each one needs its own filter",
        ),
        # 11. several series across two breakdowns: planned, not built
        ({"mode": "aggregate", "group_by": ["phase", "sponsor_class"], "series": TWO_DRUGS}, "series", "not supported yet"),
        # 12. years
        ({"mode": "aggregate", "group_by": ["phase"], "filters": {"start_year_min": 1800}}, "filters.start_year_min", "must be between 1990"),
        (
            {"mode": "aggregate", "group_by": ["phase"], "filters": {"start_year_min": 2020, "start_year_max": 2015}},
            "filters.start_year_min", "later than start_year_max",
        ),
        (
            {"mode": "aggregate", "group_by": ["phase"], "series": [{"label": "A", "filters": {"drug": "A", "start_year_max": 3000}}]},
            "series[0].filters.start_year_max", "must be between",
        ),
    ],
)
def test_bad_plan_is_rejected_with_a_plain_message(plan, path, says):
    found = issues_of(**plan)
    assert path in found, found
    assert says in found[path]


def test_every_problem_is_reported_at_once_not_just_the_first():
    found = issues_of(mode="aggregate", group_by=["gender", "nct_id"], limit=0, x_field="enrollment", series=[])
    assert set(found) == {"group_by[0]", "group_by[1]", "limit", "x_field", "series"}


def test_messages_name_the_fields_that_would_work():
    """The message goes to the LLM. Thus it must tell what is permitted, not only what is not."""
    message = issues_of(mode="points", x_field="phase", y_field="enrollment")["x_field"]
    assert message.endswith("Fields that can: enrollment, duration_months.")
    message = issues_of(mode="aggregate", group_by=["gender"])["group_by[0]"]
    assert "phase" in message and "start_year" in message and "sponsor_class" in message


def test_year_ceiling_moves_with_the_calendar():
    latest = date.today().year + 10
    assert issues_of(mode="aggregate", group_by=["phase"], filters={"start_year_max": latest}) == {}
    assert "filters.start_year_max" in issues_of(mode="aggregate", group_by=["phase"], filters={"start_year_max": latest + 1})
