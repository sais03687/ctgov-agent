"""TASK: Tests the count rules for buckets.
GRADING: Code Quality (correctness). Coverage.
"""
from collections import Counter

import pytest

from app.aggregate.aggregate import aggregate
from app.aggregate.common import Dimension, bin_start, year_range
from app.ctgov.normalize import normalize_study
from app.models import QueryPlan, SeriesFetch, Trial
from app.registry import REGISTRY, name_key, sponsor_key


def trial(number: int, **fields) -> Trial:
    return Trial(nct_id=f"NCT{number:08d}", **fields)


def fetched(trials: list[Trial], label: str = "All", **extra) -> SeriesFetch:
    return SeriesFetch(label=label, filters={}, trials=trials, trials_matched=len(trials), tier=1, **extra)


def plan(*group_by: str, **extra) -> QueryPlan:
    return QueryPlan(mode="aggregate", group_by=list(group_by), **extra)


def counts(result, key: str) -> dict:
    return {row[key]: row["trial_count"] for row in result.rows}


# ---- helpers ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Pfizer", "pfizer"),
        ("Pfizer Inc.", "pfizer"),
        ("PFIZER, INC", "pfizer"),
        ("Merck Sharp & Dohme LLC", "merck sharp dohme"),
        ("Merck Sharp & Dohme Corp.", "merck sharp dohme"),
        ("Acme Co., Ltd", "acme co"),
        ("Inc", "inc"),  # never reduced to nothing
        ("M.D. Anderson Cancer Center", "m d anderson cancer center"),
    ],
)
def test_sponsor_key(name, key):
    assert sponsor_key(name) == key


@pytest.mark.parametrize(("value", "width", "start"), [(0, 50, 0), (49, 50, 0), (50, 50, 50), (1234, 100, 1200), (7.5, 2.5, 7.5)])
def test_bin_start(value, width, start):
    assert bin_start(value, width) == start


def test_year_range_fills_the_gaps():
    assert year_range([2019, 2015, 2017]) == [2015, 2016, 2017, 2018, 2019]
    assert year_range([]) == []


def test_dimension_puts_unknown_last_in_natural_order():
    dim = Dimension(REGISTRY["phase"])
    keys = ["Unknown", "Phase 3", "Not Applicable", "Phase 1/2", "Early Phase 1", "Phase 1"]
    assert sorted(keys, key=dim.natural_position) == [
        "Early Phase 1", "Phase 1", "Phase 1/2", "Phase 3", "Not Applicable", "Unknown",
    ]


# ---- one field ----------------------------------------------------------------------------------


def test_counts_trials_per_bucket_and_records_who_was_counted():
    trials = [trial(1, phase="Phase 2"), trial(2, phase="Phase 3"), trial(3, phase="Phase 2")]
    result = aggregate([fetched(trials)], plan("phase"))
    assert result.rows == [
        {"id": "d1", "phase": "Phase 2", "trial_count": 2},
        {"id": "d2", "phase": "Phase 3", "trial_count": 1},
    ]
    assert result.contributing == {"d1": ["NCT00000001", "NCT00000003"], "d2": ["NCT00000002"]}
    assert result.trials_used == 3 and result.notes == [] and result.exact_totals is None


def test_missing_categorical_value_gets_an_explicit_unknown_bucket_and_a_note():
    trials = [trial(1, phase="Phase 2"), trial(2), trial(3)]
    result = aggregate([fetched(trials)], plan("phase"))
    assert counts(result, "phase") == {"Unknown": 2, "Phase 2": 1}
    assert result.notes == ['2 trials have no phase listed and are shown as "Unknown".']
    assert result.trials_used == 3 and result.dropped == {}


def test_explode_field_counts_a_trial_once_per_distinct_value_and_says_so():
    trials = [
        trial(1, country=["United States", "Germany"]),
        trial(2, country=["United States"]),
        trial(3, country=["Germany", "France", "United States"]),
    ]
    result = aggregate([fetched(trials)], plan("country"))
    assert counts(result, "country") == {"United States": 3, "Germany": 2, "France": 1}
    assert sum(counts(result, "country").values()) == 6 > result.trials_used == 3
    assert result.notes == [
        "Trials with more than one country are counted once per country, "
        "so the bars add up to more than the 3 trials found."
    ]


def test_no_explode_note_when_every_trial_has_a_single_value():
    trials = [trial(1, country=["Japan"]), trial(2, country=["Japan"]), trial(3, country=["Spain"])]
    assert aggregate([fetched(trials)], plan("country")).notes == []


def test_explode_field_with_no_values_goes_to_unknown():
    result = aggregate([fetched([trial(1, country=["Japan"]), trial(2)])], plan("country"))
    assert counts(result, "country") == {"Japan": 1, "Unknown": 1}


def test_boolean_field_becomes_yes_no_unknown():
    trials = [trial(1, has_results=True), trial(2, has_results=False), trial(3, has_results=False), trial(4)]
    result = aggregate([fetched(trials)], plan("has_results", sort="natural"))
    assert counts(result, "has_results") == {"No": 2, "Yes": 1, "Unknown": 1}


def test_sponsor_spellings_are_merged_and_shown_under_the_most_common_one():
    trials = [
        trial(1, sponsor="Pfizer Inc."), trial(2, sponsor="Pfizer"), trial(3, sponsor="Pfizer"),
        trial(4, sponsor="PFIZER INC"), trial(5, sponsor="Novartis"),
    ]
    result = aggregate([fetched(trials)], plan("sponsor"))
    assert counts(result, "sponsor") == {"Pfizer": 4, "Novartis": 1}
    assert result.notes == [
        "Lead sponsor names written in slightly different ways (capital letters, hyphens, punctuation, "
        "a trailing Inc. or Ltd) are counted together and shown under the most common spelling."
    ]


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("Non Small Cell Lung Cancer", "non small cell lung cancer"),
        ("Non-small Cell Lung Cancer", "non small cell lung cancer"),
        ("Non-Small  Cell Lung Cancer ", "non small cell lung cancer"),
        ("Non–Small Cell Lung Cancer", "non small cell lung cancer"),  # an en dash
        ("NSCLC", "nsclc"),  # a different word stays a different name
        ("Carcinoma, Non-Small-Cell Lung", "carcinoma, non small cell lung"),  # word order is not spelling
        ("COVID-19", "covid 19"),
    ],
)
def test_name_key(text, key):
    assert name_key(text) == key


def test_condition_spellings_that_differ_by_case_or_hyphens_are_one_bar():
    """A real case: the trials of one drug listed this condition in three ways, which gave three bars."""
    trials = (
        [trial(i, condition=["Non Small Cell Lung Cancer"]) for i in range(0, 5)]
        + [trial(i, condition=["Non-small Cell Lung Cancer"]) for i in range(5, 8)]
        + [trial(i, condition=["Non-Small Cell Lung Cancer"]) for i in range(8, 10)]
        + [trial(10, condition=["NSCLC"]), trial(11, condition=["Lung Cancer"])]
    )
    result = aggregate([fetched(trials)], plan("condition"))
    assert counts(result, "condition") == {"Non Small Cell Lung Cancer": 10, "Lung Cancer": 1, "NSCLC": 1}
    assert result.notes == [
        "Condition names written in slightly different ways (capital letters, hyphens) are counted "
        "together and shown under the most common spelling."
    ]


def test_a_trial_listing_two_spellings_of_one_name_is_counted_once():
    trials = [trial(1, intervention=["Pembrolizumab", "pembrolizumab", "Placebo"]), trial(2, intervention=["PEMBROLIZUMAB"])]
    result = aggregate([fetched(trials)], plan("intervention"))
    assert counts(result, "intervention") == {"Pembrolizumab": 2, "Placebo": 1}
    assert result.contributing[result.rows[0]["id"]] == ["NCT00000001", "NCT00000002"]


def test_no_merge_note_when_every_name_has_one_spelling():
    trials = [trial(1, condition=["Asthma"]), trial(2, condition=["Asthma"]), trial(3, condition=["COPD"])]
    assert aggregate([fetched(trials)], plan("condition")).notes == []


def test_fields_with_fixed_values_are_never_merged():
    trials = [trial(1, country=["Georgia"]), trial(2, country=["georgia"])]
    assert counts(aggregate([fetched(trials)], plan("country")), "country") == {"Georgia": 1, "georgia": 1}


def test_numeric_field_is_grouped_into_bins_and_missing_values_are_dropped():
    trials = [trial(1, enrollment=10), trial(2, enrollment=49), trial(3, enrollment=50), trial(4, enrollment=260), trial(5)]
    result = aggregate([fetched(trials)], plan("enrollment", bin={"field": "enrollment", "bin_width": 50}, sort="natural"))
    assert counts(result, "enrollment") == {"0–50": 2, "50–100": 1, "250–300": 1}
    assert result.dropped == {"enrollment": 1} and result.trials_used == 4
    assert result.notes == ["1 trial has no enrollment and is left out."]


def test_unknown_note_does_not_claim_a_bar_that_the_limit_removed():
    trials = [trial(i, phase="Phase 2") for i in range(5)] + [trial(9)]
    result = aggregate([fetched(trials)], plan("phase", limit=1))
    assert counts(result, "phase") == {"Phase 2": 5}
    assert "1 trial has no phase listed; that group is not among those shown." in result.notes


# ---- sorting and limit --------------------------------------------------------------------------


def _phase_trials():
    phases = ["Phase 3"] * 2 + ["Phase 1"] * 5 + ["Not Applicable"] * 3 + ["Phase 1/2"] * 1 + [None] * 4
    return [trial(i, phase=p) for i, p in enumerate(phases)]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("desc", ["Phase 1", "Unknown", "Not Applicable", "Phase 3", "Phase 1/2"]),
        ("asc", ["Phase 1/2", "Phase 3", "Not Applicable", "Unknown", "Phase 1"]),
        ("natural", ["Phase 1", "Phase 1/2", "Phase 3", "Not Applicable", "Unknown"]),
    ],
)
def test_sort_orders(sort, expected):
    result = aggregate([fetched(_phase_trials())], plan("phase", sort=sort))
    assert [row["phase"] for row in result.rows] == expected


def test_ties_keep_the_natural_order():
    trials = [trial(1, phase="Phase 3"), trial(2, phase="Phase 1"), trial(3, phase="Phase 2")]
    result = aggregate([fetched(trials)], plan("phase", sort="desc"))
    assert [row["phase"] for row in result.rows] == ["Phase 1", "Phase 2", "Phase 3"]


def test_limit_keeps_the_largest_buckets_and_reports_the_trim():
    trials = [trial(i, country=[c]) for i, c in enumerate("AAAAA" "BBBB" "CCC" "DD" "E")]
    result = aggregate([fetched(trials)], plan("country", limit=3))
    assert counts(result, "country") == {"A": 5, "B": 4, "C": 3}
    assert result.notes == ["5 different values of country were found; only the 3 with the most trials are shown."]
    assert result.trials_used == 15  # the trim hides bars, it does not change how many trials were read


def test_limit_with_natural_sort_keeps_the_largest_but_in_natural_order():
    result = aggregate([fetched(_phase_trials())], plan("phase", sort="natural", limit=3))
    assert [row["phase"] for row in result.rows] == ["Phase 1", "Not Applicable", "Unknown"]


# ---- time ---------------------------------------------------------------------------------------


def test_years_with_no_trials_are_zero_filled():
    trials = [trial(1, start_year=2015), trial(2, start_year=2015), trial(3, start_year=2018), trial(4, start_year=2019)]
    result = aggregate([fetched(trials)], plan("start_year"))
    assert result.rows == [
        {"id": "d1", "year": 2015, "trial_count": 2},
        {"id": "d2", "year": 2016, "trial_count": 0},
        {"id": "d3", "year": 2017, "trial_count": 0},
        {"id": "d4", "year": 2018, "trial_count": 1},
        {"id": "d5", "year": 2019, "trial_count": 1},
    ]
    assert result.contributing["d2"] == []


def test_a_time_axis_is_never_sorted_by_size_or_cut_to_the_limit():
    trials = [trial(i, start_year=2000 + i % 15) for i in range(60)]
    result = aggregate([fetched(trials)], plan("start_year", sort="desc", limit=5))
    assert [row["year"] for row in result.rows] == list(range(2000, 2015))
    assert result.notes == []


def test_trials_without_a_start_date_are_dropped_and_counted():
    trials = [trial(1, start_year=2020), trial(2), trial(3)]
    result = aggregate([fetched(trials)], plan("start_year"))
    assert counts(result, "year") == {2020: 1}
    assert result.dropped == {"start_year": 2} and result.trials_used == 1
    assert result.notes == ["2 trials have no start year and are left out."]


def test_planned_start_dates_are_counted_and_flagged():
    trials = [
        trial(1, start_year=2025, start_date_type="ACTUAL"),
        trial(2, start_year=2026, start_date_type="ESTIMATED"),
        trial(3, start_year=2027, start_date_type="ESTIMATED"),
    ]
    result = aggregate([fetched(trials)], plan("start_year"))
    assert counts(result, "year") == {2025: 1, 2026: 1, 2027: 1}  # planned trials still count
    assert result.notes == [
        "2 trials have a planned start date that ClinicalTrials.gov marks as estimated, not actual; "
        "they are counted in the planned year."
    ]


def test_one_planned_start_date_reads_in_the_singular():
    result = aggregate([fetched([trial(1, start_year=2027, start_date_type="ESTIMATED")])], plan("start_year"))
    assert result.notes == [
        "1 trial has a planned start date that ClinicalTrials.gov marks as estimated, not actual; "
        "it is counted in the planned year."
    ]


def test_no_planned_note_when_every_start_is_actual_or_the_chart_is_not_by_year():
    actual = [trial(1, start_year=2025, start_date_type="ACTUAL")]
    assert aggregate([fetched(actual)], plan("start_year")).notes == []
    planned = [trial(1, phase="Phase 2", start_year=2027, start_date_type="ESTIMATED")]
    assert aggregate([fetched(planned)], plan("phase")).notes == []


def test_planned_starts_on_real_trials_match_the_raw_json(load_fixture):
    studies = load_fixture("lung_recruiting")["studies"]
    estimated = sum(
        s["protocolSection"]["statusModule"].get("startDateStruct", {}).get("type") == "ESTIMATED" for s in studies
    )
    assert estimated > 0
    result = aggregate([fetched([normalize_study(s) for s in studies])], plan("start_year"))
    assert any(note.startswith(f"{estimated} trial") and "planned start date" in note for note in result.notes)


# ---- series and two fields ----------------------------------------------------------------------


def _two_series():
    a = fetched([trial(1, phase="Phase 1"), trial(2, phase="Phase 2"), trial(3, phase="Phase 2")], "Drug A")
    b = fetched([trial(4, phase="Phase 2"), trial(5, phase="Phase 3")], "Drug B")
    two = QueryPlan(
        mode="aggregate", group_by=["phase"], sort="natural",
        series=[{"label": "Drug A", "filters": {"drug": "A"}}, {"label": "Drug B", "filters": {"drug": "B"}}],
    )
    return [a, b], two


def test_series_give_a_complete_grid_with_zeros():
    series, two = _two_series()
    result = aggregate(series, two)
    assert [(r["phase"], r["series"], r["trial_count"]) for r in result.rows] == [
        ("Phase 1", "Drug A", 1), ("Phase 1", "Drug B", 0),
        ("Phase 2", "Drug A", 2), ("Phase 2", "Drug B", 1),
        ("Phase 3", "Drug A", 0), ("Phase 3", "Drug B", 1),
    ]
    assert result.trials_used == 5


def test_series_years_are_zero_filled_over_the_combined_range():
    a = fetched([trial(1, start_year=2015)], "A")
    b = fetched([trial(2, start_year=2017)], "B")
    two = QueryPlan(mode="aggregate", group_by=["start_year"], series=[{"label": "A"}, {"label": "B"}])
    result = aggregate([a, b], two)
    assert [(r["year"], r["series"], r["trial_count"]) for r in result.rows] == [
        (2015, "A", 1), (2015, "B", 0), (2016, "A", 0), (2016, "B", 0), (2017, "A", 0), (2017, "B", 1),
    ]


def test_a_series_that_failed_to_fetch_is_left_out_but_rows_still_name_the_series():
    series, two = _two_series()
    series[1] = SeriesFetch(label="Drug B", filters={}, error="ClinicalTrials.gov didn't respond; try again.")
    result = aggregate(series, two)
    assert {row["series"] for row in result.rows} == {"Drug A"}
    assert result.trials_used == 3


def test_two_fields_give_a_grid_of_both():
    trials = [
        trial(1, phase="Phase 2", sponsor_class="INDUSTRY"), trial(2, phase="Phase 2", sponsor_class="OTHER"),
        trial(3, phase="Phase 3", sponsor_class="INDUSTRY"), trial(4, phase="Phase 2", sponsor_class="INDUSTRY"),
    ]
    result = aggregate([fetched(trials)], plan("phase", "sponsor_class", sort="natural"))
    assert [(r["phase"], r["sponsor_class"], r["trial_count"]) for r in result.rows] == [
        ("Phase 2", "INDUSTRY", 2), ("Phase 2", "OTHER", 1), ("Phase 3", "INDUSTRY", 1), ("Phase 3", "OTHER", 0),
    ]
    assert "series" not in result.rows[0]


def test_unknown_note_counts_trials_not_cells():
    """One trial with no phase that runs in three countries is one trial with no phase."""
    trials = [trial(1, country=["A", "B", "C"]), trial(2, phase="Phase 2", country=["A"])]
    result = aggregate([fetched(trials)], plan("country", "phase"))
    assert '1 trial has no phase listed and is shown as "Unknown".' in result.notes


def test_two_fields_with_a_year_axis_use_the_year_key():
    trials = [trial(1, start_year=2020, phase="Phase 1"), trial(2, start_year=2022, phase="Phase 2")]
    result = aggregate([fetched(trials)], plan("start_year", "phase", sort="natural"))
    assert [(r["year"], r["phase"], r["trial_count"]) for r in result.rows] == [
        (2020, "Phase 1", 1), (2020, "Phase 2", 0), (2021, "Phase 1", 0), (2021, "Phase 2", 0),
        (2022, "Phase 1", 0), (2022, "Phase 2", 1),
    ]


@pytest.mark.parametrize("group_by", [[], ["phase", "status", "country"]])
def test_wrong_number_of_fields_is_refused(group_by):
    with pytest.raises(ValueError):
        aggregate([fetched([trial(1)])], QueryPlan(mode="aggregate", group_by=group_by))


def test_several_series_with_two_fields_is_refused():
    series, two = _two_series()
    with pytest.raises(ValueError):
        aggregate(series, two.model_copy(update={"group_by": ["phase", "status"]}))


def test_no_trials_gives_no_rows():
    result = aggregate([fetched([])], plan("phase"))
    assert result.rows == [] and result.trials_used == 0 and result.notes == []


# ---- tier 2 -------------------------------------------------------------------------------------


def test_tier2_uses_the_exact_counts_and_cites_the_samples():
    sample = [trial(1, phase="Phase 2"), trial(2, phase="Phase 2"), trial(3, phase="Phase 3"), trial(4)]
    series = SeriesFetch(
        label="All", filters={}, trials=sample, trials_matched=14613, tier=2, is_sample=True,
        bucket_field="phase", bucket_counts={"Phase 2": 4004, "Phase 3": 1232, "Unknown": 3212, "Phase 1": 6165},
    )
    result = aggregate([series], plan("phase", sort="natural"))
    assert counts(result, "phase") == {"Phase 1": 6165, "Phase 2": 4004, "Phase 3": 1232, "Unknown": 3212}
    by_phase = {row["phase"]: row["id"] for row in result.rows}
    assert result.contributing[by_phase["Phase 2"]] == ["NCT00000001", "NCT00000002"]
    assert result.contributing[by_phase["Phase 1"]] == []  # no sample happened to be Phase 1
    assert result.exact_totals[by_phase["Phase 2"]] == 4004
    assert result.trials_used == 14613
    assert result.notes == ['3,212 trials have no phase listed and are shown as "Unknown".']


def test_tier2_explode_field_explains_why_the_bars_exceed_the_total():
    series = SeriesFetch(
        label="All", filters={}, trials=[], trials_matched=100, tier=2, is_sample=True,
        bucket_field="intervention_type", bucket_counts={"DRUG": 80, "PROCEDURE": 40},
    )
    result = aggregate([series], plan("intervention_type"))
    assert result.notes == [
        "Trials with more than one intervention type are counted once per intervention type, "
        "so the bars add up to more than the 100 trials found."
    ]


# ---- real data ----------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["pembrolizumab_page", "lung_recruiting"])
def test_phase_counts_on_real_trials_match_a_count_of_the_raw_json(load_fixture, fixture):
    studies = load_fixture(fixture)["studies"]
    raw = Counter(tuple(s["protocolSection"]["designModule"].get("phases", [])) for s in studies)
    result = aggregate([fetched([normalize_study(s) for s in studies])], plan("phase"))
    got = counts(result, "phase")
    assert sum(got.values()) == len(studies) == 50
    assert got["Unknown"] == raw[()]
    assert got.get("Phase 1/2", 0) == raw[("PHASE1", "PHASE2")]
    assert got.get("Phase 2", 0) == raw[("PHASE2",)]
    assert all(len(result.contributing[row["id"]]) == row["trial_count"] for row in result.rows)


def test_country_counts_on_real_trials_never_count_a_trial_twice_for_one_country(load_fixture):
    studies = load_fixture("lung_recruiting")["studies"]
    result = aggregate([fetched([normalize_study(s) for s in studies])], plan("country", limit=50))
    for row in result.rows:
        ids = result.contributing[row["id"]]
        assert len(ids) == len(set(ids)) == row["trial_count"] <= 50
    assert any("counted once per country" in note for note in result.notes)
