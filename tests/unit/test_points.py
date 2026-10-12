"""TASK: Tests the scatter plot and timeline data.
GRADING: Code Quality (correctness). Coverage.
"""
import pytest

from app.aggregate.points import MAX_POINTS, points
from app.citations import build_citations
from app.ctgov.normalize import normalize_study
from app.models import ChartType, Gantt, QueryPlan, QueryRequest, ScatterPlot, SeriesFetch, Trial
from app.pipeline import Pipeline, plan_fields
from app.spec_builder import build_visualization, choose_chart
from tests.fakes import FakeCtGov, make_client

SCATTER = QueryPlan(mode="points", x_field="enrollment", y_field="duration_months")
TIMELINE = QueryPlan(mode="points", start_field="start_date", end_field="completion_date", sort="natural")


def changed(plan: QueryPlan, **updates) -> QueryPlan:
    """Returns a validated copy of a plan with some keys replaced. `filters` is merged, not replaced."""
    data = plan.model_dump()
    data["filters"].update(updates.pop("filters", {}))
    return QueryPlan.model_validate({**data, **updates})


def trial(number: int, **fields) -> Trial:
    return Trial(nct_id=f"NCT{number:08d}", **fields)


def fetched(trials, label="All", **extra) -> SeriesFetch:
    return SeriesFetch(label=label, filters={}, trials=trials, trials_matched=len(trials), tier=1, **extra)


# ---- scatter ------------------------------------------------------------------------------------


def test_scatter_has_one_dot_per_trial_with_both_values():
    trials = [trial(2, enrollment=100, duration_months=24.0), trial(1, enrollment=40, duration_months=6.5)]
    result = points([fetched(trials)], SCATTER)
    assert result.rows == [
        {"id": "d1", "nct_id": "NCT00000001", "enrollment": 40, "duration_months": 6.5},
        {"id": "d2", "nct_id": "NCT00000002", "enrollment": 100, "duration_months": 24.0},
    ]
    assert result.contributing == {"d1": ["NCT00000001"], "d2": ["NCT00000002"]}
    assert result.trials_used == 2 and result.notes == []


def test_trials_missing_either_value_are_dropped_and_counted():
    trials = [trial(1, enrollment=40, duration_months=6.5), trial(2, enrollment=10), trial(3, duration_months=3.0), trial(4)]
    result = points([fetched(trials)], SCATTER)
    assert [row["nct_id"] for row in result.rows] == ["NCT00000001"]
    assert result.dropped == {"enrollment or duration_months": 3}
    assert result.notes == ["3 trials have no enrollment or duration and are left out."]


def test_zero_is_a_value_not_a_missing_one():
    result = points([fetched([trial(1, enrollment=0, duration_months=0.0)])], SCATTER)
    assert len(result.rows) == 1 and result.dropped == {}


def test_more_trials_than_can_be_drawn_are_thinned_evenly_and_repeatably():
    trials = [trial(i, enrollment=i, duration_months=float(i)) for i in range(1, 2001)]
    result = points([fetched(trials)], SCATTER)
    assert len(result.rows) == MAX_POINTS == result.trials_used
    shown = [row["enrollment"] for row in result.rows]
    assert shown[0] == 1 and shown[-1] > 1990  # spread over the whole set, not the first 500
    assert shown == sorted(shown) and shown[1] - shown[0] == 4
    assert result.notes == ["2,000 trials could be plotted; 500 of them, spread evenly by registration number, are shown."]
    assert points([fetched(list(reversed(trials)))], SCATTER).rows == result.rows  # input order is irrelevant


def test_planned_completion_dates_are_flagged_when_duration_is_plotted():
    trials = [
        trial(1, enrollment=10, duration_months=12.0, completion_date_type="ACTUAL"),
        trial(2, enrollment=20, duration_months=30.0, completion_date_type="ESTIMATED"),
        trial(3, enrollment=30, duration_months=40.0, completion_date_type="ESTIMATED"),
    ]
    result = points([fetched(trials)], SCATTER)
    assert result.notes == [
        "2 of the 3 trials shown have a planned completion date, not an actual one, so their duration is an estimate."
    ]


def test_scatter_of_several_series_marks_each_dot_with_its_series():
    two = changed(SCATTER, series=[{"label": "A", "filters": {"drug": "A"}}, {"label": "B", "filters": {"drug": "B"}}])
    a = fetched([trial(1, enrollment=10, duration_months=1.0)], "A")
    b = fetched([trial(2, enrollment=20, duration_months=2.0)], "B")
    result = points([a, b], two)
    assert [(row["series"], row["nct_id"]) for row in result.rows] == [("A", "NCT00000001"), ("B", "NCT00000002")]
    viz = build_visualization(two, result, choose_chart(two, result, None)[0])
    assert viz.encoding.series.field == "series" and viz.title == "Trials: enrollment vs duration (A vs B)"


def test_scatter_on_real_trials_uses_the_recorded_values(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    raw = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in studies}
    series = fetched([normalize_study(s) for s in studies])
    result = points([series], SCATTER)
    assert len(result.rows) + sum(result.dropped.values()) == 50
    for row in result.rows:
        assert row["enrollment"] == raw[row["nct_id"]]["protocolSection"]["designModule"]["enrollmentInfo"]["count"]
        assert row["duration_months"] >= 0


def test_a_dot_cites_its_trial_with_the_values_behind_both_axes(load_fixture):
    studies = load_fixture("placebo_arms")["studies"]
    series = fetched([normalize_study(s) for s in studies])
    result = points([series], SCATTER)
    [cited] = build_citations(result, [series], SCATTER, 10).values()
    assert (cited.total, cited.shown, cited.search_is_exact) == (1, 1, True)
    assert cited.search_url == "https://clinicaltrials.gov/search?term=AREA%5BNCTId%5DNCT04003636"
    [item] = cited.items
    assert item.bucket_label == "NCT04003636"
    assert [(e.field.rsplit(".", 2)[-2:], e.value) for e in item.evidence] == [
        (["enrollmentInfo", "count"], item.evidence[0].value),
        (["startDateStruct", "date"], "2019-09-24"),  # duration is computed from these two dates
        (["completionDateStruct", "date"], "2025-04-01"),
    ]


def test_scatter_chart_spec():
    plan = changed(SCATTER, filters={"condition": "Alzheimer's disease", "phase": ["PHASE3"]})
    result = points([fetched([trial(1, enrollment=40, duration_months=6.5)])], plan)
    chart, note = choose_chart(plan, result, None)
    viz = build_visualization(plan, result, chart)
    assert isinstance(viz, ScatterPlot) and note is None
    assert viz.title == "Phase 3 Alzheimer's disease trials: enrollment vs duration"
    assert viz.encoding.x.model_dump() == {"field": "enrollment", "type": "quantitative", "title": "Enrollment", "unit": "participants"}
    assert viz.encoding.y.model_dump() == {"field": "duration_months", "type": "quantitative", "title": "Duration", "unit": "months"}
    assert viz.encoding.series is None
    assert all(channel.field in viz.data[0] for channel in (viz.encoding.x, viz.encoding.y))


# ---- timeline (gantt) ---------------------------------------------------------------------------

DATED = [
    trial(1, start_date="2020-03-01", completion_date="2022-06-30"),
    trial(2, start_date="2018-01", completion_date="2019-12", brief_title="A second study"),  # the others have no title
    trial(3, start_date="2021-07-15", completion_date="2027-01-01", completion_date_type="ESTIMATED"),
]


def test_timeline_has_one_bar_per_trial_earliest_first():
    result = points([fetched(DATED)], TIMELINE)
    assert result.rows == [
        {"id": "d1", "nct_id": "NCT00000002", "label": "A second study", "start": "2018-01", "end": "2019-12"},
        {"id": "d2", "nct_id": "NCT00000001", "label": "NCT00000001", "start": "2020-03-01", "end": "2022-06-30"},
        {"id": "d3", "nct_id": "NCT00000003", "label": "NCT00000003", "start": "2021-07-15", "end": "2027-01-01"},
    ]
    assert result.notes == [
        "1 of the 3 trials shown have a planned completion date, not an actual one, so their bar ends at a planned date."
    ]


def test_timeline_sorted_desc_shows_the_most_recent_starts():
    result = points([fetched(DATED)], changed(TIMELINE, sort="desc", limit=2))
    assert [row["nct_id"] for row in result.rows] == ["NCT00000003", "NCT00000001"]
    assert "3 trials have both dates; the 2 most recently started are shown." in result.notes


def test_timeline_is_cut_to_the_limit_and_says_which_trials_were_kept():
    trials = [trial(i, start_date=f"20{10 + i % 10}-01-01", completion_date="2030-01-01") for i in range(30)]
    result = points([fetched(trials)], changed(TIMELINE, limit=5))
    assert len(result.rows) == 5 and {row["start"] for row in result.rows} == {"2010-01-01", "2011-01-01"}
    assert "30 trials have both dates; the 5 earliest to start are shown." in result.notes
    assert result.dropped == {"over the limit": 25}


def test_trials_without_both_dates_or_with_dates_in_the_wrong_order_are_left_out():
    trials = [
        trial(1, start_date="2020-01-01", completion_date="2021-01-01"),
        trial(2, start_date="2020-01-01"),
        trial(3, completion_date="2021-01-01"),
        trial(4, start_date="2022-01-01", completion_date="2021-01-01"),
    ]
    result = points([fetched(trials)], TIMELINE)
    assert [row["nct_id"] for row in result.rows] == ["NCT00000001"]
    assert result.notes == ["3 trials have no start date or no completion date (or dates in the wrong order) and are left out."]


def test_dates_of_different_precision_in_the_same_month_are_not_called_out_of_order():
    result = points([fetched([trial(1, start_date="2020-05-17", completion_date="2020-05")])], TIMELINE)
    assert len(result.rows) == 1


def test_timeline_chart_spec_and_citation(load_fixture):
    series = fetched([normalize_study(s) for s in load_fixture("placebo_arms")["studies"]])
    plan = changed(TIMELINE, filters={"drug": "pembrolizumab", "start_year_min": 2019})
    result = points([series], plan)
    viz = build_visualization(plan, result, choose_chart(plan, result, None)[0])
    assert isinstance(viz, Gantt)
    assert viz.title == "Pembrolizumab trials since 2019: start to completion"
    study = load_fixture("placebo_arms")["studies"][0]
    title = study["protocolSection"]["identificationModule"]["briefTitle"]
    assert viz.data[0].model_dump() == {"id": "d1", "nct_id": "NCT04003636", "label": title, "start": "2019-09-24", "end": "2025-04-01"}
    assert len(title) > 60  # real titles are long; the full text is given and the renderer shortens it
    assert (viz.encoding.y.field, viz.encoding.start.field, viz.encoding.end.field) == ("label", "start", "end")
    assert (viz.encoding.start.type, viz.encoding.start.title, viz.encoding.end.title) == ("temporal", "Start date", "Completion date")
    [cited] = build_citations(result, [series], plan, 10).values()
    assert [e.value for e in cited.items[0].evidence] == ["2019-09-24", "2025-04-01"]


# ---- overrides and the pipeline -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "suggested", "note"),
    [
        (SCATTER, "bar_chart", "A bar chart was asked for, but enrollment against duration for each trial is shown as a scatter plot, which fits this data."),
        (SCATTER, "gantt", "A gantt was asked for, but enrollment against duration for each trial is shown as a scatter plot, which fits this data."),
        (TIMELINE, "time_series", "A time series was asked for, but the start and end of each trial are shown as a gantt, which fits this data."),
    ],
)
def test_a_chart_that_does_not_fit_points_is_overridden_with_a_note(plan, suggested, note):
    result = points([fetched([])], plan)
    expected = ChartType.SCATTER_PLOT if plan.x_field else ChartType.GANTT
    assert choose_chart(plan, result, ChartType(suggested)) == (expected, note)


def test_points_plans_fetch_the_fields_they_plot():
    assert plan_fields(SCATTER) == ["enrollment", "duration_months"]
    assert plan_fields(TIMELINE) == ["start_date", "completion_date", "brief_title"]


async def test_scatter_through_the_pipeline(load_fixture):
    studies = load_fixture("lung_recruiting")["studies"]
    request = QueryRequest(query="do bigger trials take longer?", max_citations=1)
    response = await Pipeline(make_client(FakeCtGov(studies))).run_plan(request, SCATTER)
    assert response.status == "ok" and response.visualization.type == "scatter_plot"
    assert set(response.citations) == {row["id"] for row in response.visualization.data}
    assert response.meta.units == {"x": "participants", "y": "months"} and response.meta.sort is None
    assert any("planned completion date" in note for note in response.meta.notes)  # recruiting trials are ongoing


async def test_timeline_through_the_pipeline_with_an_overridden_suggestion(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    request = QueryRequest(query="timeline of pembrolizumab trials", chart_type="bar_chart")
    response = await Pipeline(make_client(FakeCtGov(studies))).run_plan(request, TIMELINE)
    assert response.visualization.type == "gantt" and len(response.visualization.data) == 10
    assert response.meta.chart_overridden is True and response.meta.notes[0].startswith("A bar chart was asked for")
    starts = [row.start for row in response.visualization.data]
    assert starts == sorted(starts)
    titles = {s["protocolSection"]["identificationModule"]["nctId"]: s["protocolSection"]["identificationModule"]["briefTitle"] for s in studies}
    assert all(row.label == titles[row.nct_id] for row in response.visualization.data)


async def test_trials_matched_but_none_plottable_is_not_reported_as_no_trials():
    studies = [{"protocolSection": {"identificationModule": {"nctId": f"NCT0000000{i}"}}} for i in range(3)]
    response = await Pipeline(make_client(FakeCtGov(studies))).run_plan(QueryRequest(query="size against duration"), SCATTER)
    assert response.status == "ok" and response.visualization.data == []
    assert response.meta.notes == ["3 trials have no enrollment or duration and are left out."]
