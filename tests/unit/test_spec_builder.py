"""TASK: Tests the chart selection, the encodings, and the titles.
GRADING: Input/Output Design. Coverage.
"""
import pytest

from app.models import AggregateResult, ChartType, QueryPlan
from app.spec_builder import build_title, build_visualization, choose_chart, default_chart

TWO_SERIES = [{"label": "Drug A", "filters": {"drug": "A"}}, {"label": "Drug B", "filters": {"drug": "B"}}]


def rows_result(rows) -> AggregateResult:
    return AggregateResult(mode="aggregate", rows=rows)


def stack_rows(n_values: int):
    return [{"id": f"d{i}", "phase": "Phase 2", "country": f"C{i}", "trial_count": 1} for i in range(n_values)]


# ---- selection ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("group_by", "series", "rows", "expected"),
    [
        (["phase"], None, [], ChartType.BAR_CHART),
        (["start_year"], None, [], ChartType.TIME_SERIES),
        (["phase"], TWO_SERIES, [], ChartType.GROUPED_BAR_CHART),
        (["start_year"], TWO_SERIES, [], ChartType.TIME_SERIES),
        (["phase", "country"], None, stack_rows(8), ChartType.STACKED_BAR_CHART),
        (["phase", "country"], None, stack_rows(9), ChartType.HEATMAP),
    ],
)
def test_chart_follows_the_shape_of_the_data(group_by, series, rows, expected):
    plan = QueryPlan(mode="aggregate", group_by=group_by, **({"series": series} if series else {}))
    assert default_chart(plan, rows_result(rows)) is expected


def test_no_suggestion_or_a_matching_one_needs_no_note():
    plan = QueryPlan(mode="aggregate", group_by=["phase"])
    assert choose_chart(plan, rows_result([]), None) == (ChartType.BAR_CHART, None)
    assert choose_chart(plan, rows_result([]), ChartType.BAR_CHART) == (ChartType.BAR_CHART, None)


@pytest.mark.parametrize(
    ("group_by", "series", "rows", "suggested"),
    [
        (["start_year"], None, [], ChartType.BAR_CHART),  # years as bars is a fair choice
        (["start_year"], TWO_SERIES, [], ChartType.GROUPED_BAR_CHART),
        (["phase", "country"], None, stack_rows(3), ChartType.HEATMAP),
    ],
)
def test_a_suggestion_that_also_fits_is_honoured(group_by, series, rows, suggested):
    plan = QueryPlan(mode="aggregate", group_by=group_by, **({"series": series} if series else {}))
    assert choose_chart(plan, rows_result(rows), suggested) == (suggested, None)


@pytest.mark.parametrize(
    ("group_by", "series", "rows", "suggested", "chosen"),
    [
        (["phase"], None, [], ChartType.SCATTER_PLOT, ChartType.BAR_CHART),
        (["phase"], None, [], ChartType.TIME_SERIES, ChartType.BAR_CHART),
        (["phase"], TWO_SERIES, [], ChartType.BAR_CHART, ChartType.GROUPED_BAR_CHART),
        (["start_year"], None, [], ChartType.GROUPED_BAR_CHART, ChartType.TIME_SERIES),
        (["phase", "country"], None, stack_rows(9), ChartType.STACKED_BAR_CHART, ChartType.HEATMAP),
        (["phase"], None, [], ChartType.NETWORK_GRAPH, ChartType.BAR_CHART),
    ],
)
def test_a_suggestion_that_does_not_fit_is_replaced_with_a_note(group_by, series, rows, suggested, chosen):
    plan = QueryPlan(mode="aggregate", group_by=group_by, **({"series": series} if series else {}))
    chart, note = choose_chart(plan, rows_result(rows), suggested)
    assert chart is chosen
    assert suggested.value.replace("_", " ") in note and chosen.value.replace("_", " ") in note


def test_override_note_is_plain_language():
    plan = QueryPlan(mode="aggregate", group_by=["phase"])
    _, note = choose_chart(plan, rows_result([]), ChartType.SCATTER_PLOT)
    assert note == (
        "A scatter plot was asked for, but counts of trials by phase are shown as a bar chart, "
        "which fits this data."
    )


# ---- encodings ----------------------------------------------------------------------------------


def test_bar_chart_encoding_points_at_the_row_keys():
    plan = QueryPlan(mode="aggregate", group_by=["phase"])
    rows = [{"id": "d1", "phase": "Phase 2", "trial_count": 3}]
    chart = build_visualization(plan, rows_result(rows), ChartType.BAR_CHART).model_dump(mode="json")
    assert chart["type"] == "bar_chart"
    assert chart["encoding"] == {
        "x": {"field": "phase", "type": "categorical", "title": "Phase", "unit": None},
        "y": {"field": "trial_count", "type": "quantitative", "title": "Trials", "unit": "trials"},
    }
    assert chart["data"] == rows


def test_time_series_encoding_is_temporal_and_uses_the_year_key():
    plan = QueryPlan(mode="aggregate", group_by=["start_year"])
    chart = build_visualization(plan, rows_result([]), ChartType.TIME_SERIES)
    assert (chart.encoding.x.field, chart.encoding.x.type, chart.encoding.x.title) == ("year", "temporal", "Start year")
    assert chart.encoding.series is None


def test_multi_series_charts_carry_a_series_channel():
    plan = QueryPlan(mode="aggregate", group_by=["start_year"], series=TWO_SERIES)
    assert build_visualization(plan, rows_result([]), ChartType.TIME_SERIES).encoding.series.field == "series"
    plan = QueryPlan(mode="aggregate", group_by=["phase"], series=TWO_SERIES)
    assert build_visualization(plan, rows_result([]), ChartType.GROUPED_BAR_CHART).encoding.series.field == "series"


def test_stacked_and_heatmap_encodings_use_the_second_field():
    plan = QueryPlan(mode="aggregate", group_by=["phase", "sponsor_class"])
    stacked = build_visualization(plan, rows_result([]), ChartType.STACKED_BAR_CHART)
    assert (stacked.encoding.x.field, stacked.encoding.stack.field) == ("phase", "sponsor_class")
    heatmap = build_visualization(plan, rows_result([]), ChartType.HEATMAP)
    assert (heatmap.encoding.x.field, heatmap.encoding.y.field, heatmap.encoding.color.field) == (
        "phase", "sponsor_class", "trial_count",
    )


def test_every_encoding_field_is_a_key_of_the_rows():
    plan = QueryPlan(mode="aggregate", group_by=["phase", "sponsor_class"])
    rows = [{"id": "d1", "phase": "Phase 2", "sponsor_class": "INDUSTRY", "trial_count": 3}]
    chart = build_visualization(plan, rows_result(rows), ChartType.STACKED_BAR_CHART)
    for channel in (chart.encoding.x, chart.encoding.y, chart.encoding.stack):
        assert channel.field in rows[0]


def test_a_chart_type_without_a_builder_is_an_error_not_a_wrong_chart():
    with pytest.raises(ValueError):
        build_visualization(QueryPlan(mode="aggregate", group_by=["phase", "status"]), rows_result([]), ChartType.NETWORK_GRAPH)


# ---- titles -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "title"),
    [
        ({"filters": {"drug": "pembrolizumab"}, "group_by": ["phase"]}, "Pembrolizumab trials by phase"),
        (
            {"filters": {"condition": "lung cancer", "status": ["RECRUITING"]}, "group_by": ["country"]},
            "Recruiting lung cancer trials by country",
        ),
        (
            {"filters": {"condition": "melanoma", "status": ["RECRUITING", "COMPLETED"]}, "group_by": ["phase"]},
            "Melanoma trials by phase",  # two statuses: left to meta.filters_applied
        ),
        (
            {"filters": {"condition": "asthma", "start_year_min": 2015}, "group_by": ["start_year"]},
            "Asthma trials by start year since 2015",
        ),
        (
            {"filters": {"sponsor": "Pfizer", "start_year_min": 2010, "start_year_max": 2020}, "group_by": ["phase"]},
            "Pfizer trials by phase, 2010–2020",
        ),
        (
            {"filters": {"condition": "diabetes", "country": "Germany"}, "group_by": ["phase", "sponsor_class"]},
            "Diabetes trials in Germany by phase and sponsor category",
        ),
        ({"group_by": ["phase"], "series": TWO_SERIES}, "Trials by phase: Drug A vs Drug B"),
        ({"filters": {"status": ["ACTIVE_NOT_RECRUITING"]}, "group_by": ["phase"]}, "Active, not recruiting trials by phase"),
        ({"filters": {"condition": "asthma", "phase": ["PHASE3"]}, "group_by": ["country"]}, "Phase 3 asthma trials by country"),
        (
            {"filters": {"condition": "asthma", "phase": ["PHASE3"], "status": ["RECRUITING"]}, "group_by": ["country"]},
            "Recruiting Phase 3 asthma trials by country",
        ),
        ({"filters": {"condition": "asthma", "phase": ["PHASE2", "PHASE3"]}, "group_by": ["country"]}, "Asthma trials by country"),
    ],
)
def test_title_is_built_from_the_plan(plan, title):
    assert build_title(QueryPlan(mode="aggregate", **plan)) == title
