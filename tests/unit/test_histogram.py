"""TASK: Tests the histogram bins and the overflow bar.
GRADING: Code Quality (correctness). Coverage.
"""
import pytest

from app.aggregate.histogram import MAX_BINS, histogram, nice_width
from app.citations import build_citations
from app.ctgov.normalize import normalize_study
from app.models import ChartType, Histogram, QueryPlan, QueryRequest, SeriesFetch, Trial
from app.pipeline import Pipeline
from app.spec_builder import build_visualization, choose_chart
from tests.fakes import FakeCtGov, make_client


def trials_with(field: str, values: list) -> list[Trial]:
    return [Trial(nct_id=f"NCT{i:08d}", **({field: v} if v is not None else {})) for i, v in enumerate(values)]


def fetched(trials, **extra) -> SeriesFetch:
    return SeriesFetch(label="All", filters={}, trials=trials, trials_matched=len(trials), tier=1, **extra)


def plan(field="enrollment", width=50, **extra) -> QueryPlan:
    return QueryPlan(mode="histogram", bin={"field": field, "bin_width": width}, **extra)


def bars(result) -> list[tuple]:
    return [(r["bin_start"], r["bin_end"], r["trial_count"]) for r in result.rows]


# ---- binning ------------------------------------------------------------------------------------


def test_trials_are_counted_per_bin_with_lower_edge_inclusive():
    result = histogram([fetched(trials_with("enrollment", [0, 10, 49, 50, 99, 100]))], plan(width=50))
    assert bars(result) == [(0, 50, 3), (50, 100, 2), (100, 150, 1)]
    assert result.trials_used == 6 and result.notes == []


def test_empty_bins_between_values_are_kept_as_zero():
    result = histogram([fetched(trials_with("enrollment", [5, 12, 230]))], plan(width=50))
    assert bars(result) == [(0, 50, 2), (50, 100, 0), (100, 150, 0), (150, 200, 0), (200, 250, 1)]


def test_bins_start_at_the_bin_holding_the_smallest_value_not_at_zero():
    result = histogram([fetched(trials_with("enrollment", [120, 130, 260]))], plan(width=100))
    assert bars(result) == [(100, 200, 2), (200, 300, 1)]


def test_every_bar_records_the_trials_in_it():
    result = histogram([fetched(trials_with("enrollment", [10, 60, 70]))], plan(width=50))
    assert result.contributing == {"d1": ["NCT00000000"], "d2": ["NCT00000001", "NCT00000002"]}
    assert [result.about[i].label for i in ("d1", "d2")] == ["0–50 participants", "50–100 participants"]


def test_trials_without_a_value_are_dropped_and_counted():
    result = histogram([fetched(trials_with("enrollment", [10, None, None, 20]))], plan())
    assert bars(result) == [(0, 50, 2)]
    assert result.dropped == {"enrollment": 2} and result.trials_used == 2
    assert result.notes == ["2 trials have no enrollment and are left out."]


def test_no_usable_values_gives_no_bars():
    result = histogram([fetched(trials_with("enrollment", [None]))], plan())
    assert result.rows == [] and result.notes == ["1 trial has no enrollment and is left out."]
    assert histogram([fetched([])], plan()).rows == []


def test_fractional_values_and_widths_work():
    result = histogram([fetched(trials_with("duration_months", [0.5, 5.9, 6.0, 13.2]))], plan("duration_months", 6))
    assert bars(result) == [(0, 6, 2), (6, 12, 1), (12, 18, 1)]
    assert result.about["d1"].label == "0–6 months"


# ---- the long tail ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("needed", "width"),
    [(0.3, 0.5), (1, 1), (1.2, 2), (3, 5), (7, 10), (12, 20), (26, 50), (51, 100), (480, 500), (1234, 2000), (0, 1)],
)
def test_nice_width(needed, width):
    assert nice_width(needed) == width


def test_values_beyond_the_last_regular_bar_go_into_one_overflow_bar():
    values = list(range(0, 3900, 10)) + [250_000, 1_000_000]  # two huge registry studies
    result = histogram([fetched(trials_with("enrollment", values))], plan(width=100))
    assert len(result.rows) == MAX_BINS
    last = result.rows[-1]
    assert (last["bin_start"], last["bin_end"], last["trial_count"], last["is_overflow"]) == (3900, 1_000_000, 2, True)
    assert all(row["is_overflow"] is False for row in result.rows[:-1])
    assert result.notes == ["2 trials have 3,900 participants or more and are gathered in the last bar."]
    assert result.about[last["id"]].label == "3,900 participants or more"
    assert sum(row["trial_count"] for row in result.rows) == len(values) == result.trials_used


def test_a_width_too_fine_for_the_data_is_widened_and_says_so():
    values = list(range(0, 4000, 4))  # 1,000 trials up to 4,000 participants, asked in bars of 10
    result = histogram([fetched(trials_with("enrollment", values))], plan(width=10))
    assert len(result.rows) <= MAX_BINS
    assert result.rows[0]["bin_end"] - result.rows[0]["bin_start"] == 100
    assert result.notes[0] == "The bar width was changed from 10 to 100 participants, so that the chart has at most 40 bars."
    assert sum(row["trial_count"] for row in result.rows) == 1000


def test_a_fitting_width_is_left_alone():
    result = histogram([fetched(trials_with("enrollment", list(range(0, 1000, 7))))], plan(width=50))
    assert result.rows[0]["bin_end"] == 50 and result.notes == []
    assert not any(row["is_overflow"] for row in result.rows)


def test_outliers_alone_do_not_force_a_wider_width():
    """95 % of the trials fit. The very large value goes to the overflow bar and does not make the chart flat."""
    values = [20] * 97 + [40, 60] + [1_000_000]
    result = histogram([fetched(trials_with("enrollment", values))], plan(width=10))
    assert result.rows[0]["bin_end"] - result.rows[0]["bin_start"] == 10
    assert result.rows[-1]["is_overflow"] and result.rows[-1]["trial_count"] == 1


# ---- real data ----------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["pembrolizumab_page", "lung_recruiting"])
def test_real_enrollment_values_land_in_their_bars(load_fixture, fixture):
    studies = load_fixture(fixture)["studies"]
    raw = {s["protocolSection"]["identificationModule"]["nctId"]: s["protocolSection"]["designModule"]["enrollmentInfo"]["count"] for s in studies}
    result = histogram([fetched([normalize_study(s) for s in studies])], plan(width=100))
    assert sum(row["trial_count"] for row in result.rows) == 50
    for row in result.rows:
        for nct_id in result.contributing[row["id"]]:
            if row["is_overflow"]:
                assert raw[nct_id] >= row["bin_start"]
            else:
                assert row["bin_start"] <= raw[nct_id] < row["bin_end"]


# ---- citations, chart and response --------------------------------------------------------------


def test_bars_cite_the_raw_enrollment_and_link_to_an_exact_range(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    series = fetched([normalize_study(s) for s in studies], api_params={"query.intr": "pembrolizumab"})
    result = histogram([series], plan(width=100))
    citations = build_citations(result, [series], plan(width=100), 3)
    assert set(citations) == {row["id"] for row in result.rows}
    first = citations["d1"]
    assert first.total == result.rows[0]["trial_count"] and first.search_is_exact is True
    assert "term=AREA%5BEnrollmentCount%5DRANGE%5B0%2C99%5D" in first.search_url and "intr=pembrolizumab" in first.search_url
    item = first.items[0]
    assert item.evidence[0].field == "protocolSection.designModule.enrollmentInfo.count"
    assert 0 <= item.evidence[0].value < 100 and item.bucket_label == "0–100 participants"


def test_overflow_bar_links_to_an_open_ended_range():
    series = fetched(trials_with("enrollment", list(range(0, 3900, 10)) + [9_999_999]))
    result = histogram([series], plan(width=100))
    citations = build_citations(result, [series], plan(width=100), 1)
    assert "RANGE%5B3900%2CMAX%5D" in citations[result.rows[-1]["id"]].search_url


def test_a_derived_field_has_no_search_so_its_link_is_marked_inexact():
    series = fetched(trials_with("duration_months", [3.0, 14.5]), api_params={"query.cond": "asthma"})
    result = histogram([series], plan("duration_months", 6))
    cited = build_citations(result, [series], plan("duration_months", 6), 1)["d1"]
    assert cited.search_is_exact is False and cited.search_url.endswith("search?cond=asthma")


def test_histogram_chart_spec():
    the_plan = plan(width=50, filters={"condition": "breast cancer"})
    result = histogram([fetched(trials_with("enrollment", [10, 60]))], the_plan)
    chart, note = choose_chart(the_plan, result, None)
    viz = build_visualization(the_plan, result, chart)
    assert isinstance(viz, Histogram) and note is None
    assert viz.title == "Breast cancer trials by enrollment"
    assert viz.encoding.x.model_dump() == {"field": "bin_start", "type": "quantitative", "title": "Enrollment", "unit": "participants"}
    assert viz.data[0].model_dump() == {"id": "d1", "bin_start": 0.0, "bin_end": 50.0, "trial_count": 1, "is_overflow": False}


def test_a_bar_chart_is_drawn_when_a_bar_chart_is_asked_for():
    """The same counts and IDs, one labelled bar for each range, and no chart replacement."""
    the_plan = plan(width=100, filters={"condition": "asthma"})
    values = list(range(0, 3900, 10)) + [250_000]
    series = fetched(trials_with("enrollment", values))
    result = histogram([series], the_plan)
    chart, note = choose_chart(the_plan, result, ChartType.BAR_CHART)
    assert (chart, note) == (ChartType.BAR_CHART, None)

    viz = build_visualization(the_plan, result, chart)
    assert viz.type == "bar_chart" and viz.title == "Asthma trials by enrollment"
    assert viz.encoding.x.model_dump() == {"field": "enrollment", "type": "categorical", "title": "Enrollment", "unit": "participants"}
    assert viz.data[0] == {"id": "d1", "enrollment": "0–100", "trial_count": 10}
    assert viz.data[1]["enrollment"] == "100–200" and viz.data[-1] == {"id": "d40", "enrollment": "3,900 or more", "trial_count": 1}
    # the bars are the histogram's bars, so its citations apply unchanged
    assert [row["id"] for row in viz.data] == [row["id"] for row in result.rows]
    assert set(build_citations(result, [series], the_plan, 1)) == {row["id"] for row in viz.data}


async def test_bar_chart_request_goes_through_the_pipeline_without_an_override(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    request = QueryRequest(query="bar chart of how big pembrolizumab trials are", chart_type="bar_chart")
    response = await Pipeline(make_client(FakeCtGov(studies))).run_plan(request, plan(width=100))
    assert response.visualization.type == "bar_chart"
    assert response.meta.chart_overridden is False and not any("asked for" in note for note in response.meta.notes)
    assert sum(row["trial_count"] for row in response.visualization.data) == 50
    assert (response.meta.sort, response.meta.units) == ("natural", {"x": "participants", "y": "trials"})
    assert set(response.citations) == {row["id"] for row in response.visualization.data}


def test_asking_for_a_chart_that_cannot_show_a_distribution_is_overridden_with_a_note():
    the_plan = plan()
    result = histogram([fetched(trials_with("enrollment", [10]))], the_plan)
    for suggested in ("scatter_plot", "time_series", "network_graph"):
        chart, note = choose_chart(the_plan, result, ChartType(suggested))
        assert chart is ChartType.HISTOGRAM
        assert note == (
            f"A {suggested.replace('_', ' ')} was asked for, but the spread of enrollment is shown as a "
            "histogram, which fits this data."
        )


async def test_histogram_through_the_pipeline(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    the_plan = plan(width=100, filters={"drug": "pembrolizumab"})
    request = QueryRequest(query="how big are pembrolizumab trials?", max_citations=2)
    response = await Pipeline(make_client(FakeCtGov(studies))).run_plan(request, the_plan)
    assert response.status == "ok" and response.visualization.type == "histogram"
    assert set(response.citations) == {row.id for row in response.visualization.data}
    assert (response.meta.sort, response.meta.units) == ("natural", {"x": "participants", "y": "trials"})
    assert response.meta.trials_used == 50
