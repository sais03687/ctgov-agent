"""TASK: Tests the change from a raw study record to a Trial, with real API responses.
GRADING: System Design (real API data).
"""
import re

import pytest

from app.ctgov.normalize import display_label, months_between, normalize_study, phase_label
from app.registry import REGISTRY, resolve_path

STUDY_FIXTURES = ["pembrolizumab_page", "phase12", "placebo_arms", "lung_recruiting"]


def value_at(study, indexed_path):
    """Follows an evidence path such as `a.b[3].c` through the raw study."""
    node = study
    for key, index in re.findall(r"([^.\[\]]+)(?:\[(\d+)\])?", indexed_path):
        node = node[key]
        if index:
            node = node[int(index)]
    return node


# ---- all fixtures -------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", STUDY_FIXTURES)
def test_every_fixture_study_normalizes(load_fixture, fixture):
    studies = load_fixture(fixture)["studies"]
    trials = [normalize_study(study) for study in studies]
    assert [t.nct_id for t in trials] == [
        s["protocolSection"]["identificationModule"]["nctId"] for s in studies
    ]


@pytest.mark.parametrize("fixture", STUDY_FIXTURES)
def test_evidence_is_the_raw_value_found_at_its_path(load_fixture, fixture):
    for study in load_fixture(fixture)["studies"]:
        trial = normalize_study(study)
        assert set(trial.evidence) <= set(REGISTRY)
        for entries in trial.evidence.values():
            for entry in entries:
                assert value_at(study, entry.path) == entry.raw


@pytest.mark.parametrize("fixture", STUDY_FIXTURES)
def test_explode_fields_have_one_evidence_entry_per_distinct_value(load_fixture, fixture):
    for study in load_fixture(fixture)["studies"]:
        trial = normalize_study(study)
        for name in ["country", "condition", "intervention", "intervention_type"]:
            values = getattr(trial, name)
            assert len(values) == len(set(values))
            assert [display_label(name, e.raw) for e in trial.evidence.get(name, [])] == values


@pytest.mark.parametrize("fixture", STUDY_FIXTURES)
def test_a_value_is_present_exactly_when_it_has_evidence(load_fixture, fixture):
    for study in load_fixture(fixture)["studies"]:
        trial = normalize_study(study)
        for name in REGISTRY:
            assert (getattr(trial, name) not in (None, [])) == (name in trial.evidence), name


# ---- phase (combine) ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        (["PHASE1", "PHASE2"], "Phase 1/2"),
        (["PHASE2", "PHASE1"], "Phase 1/2"),
        (["PHASE2", "PHASE3"], "Phase 2/3"),
        (["PHASE3"], "Phase 3"),
        (["EARLY_PHASE1"], "Early Phase 1"),
        (["NA"], "Not Applicable"),
        (["EARLY_PHASE1", "PHASE1"], "Early Phase 1 / Phase 1"),
        (["SOMETHING_NEW"], "SOMETHING_NEW"),
        ([], None),
        (None, None),
    ],
)
def test_phase_label(raw, label):
    assert phase_label(raw) == label


def test_phase_1_and_2_combine_into_one_label_and_keep_the_raw_list(load_fixture):
    for study in load_fixture("phase12")["studies"]:
        trial = normalize_study(study)
        assert trial.phase == "Phase 1/2"
        [evidence] = trial.evidence["phase"]
        assert evidence.path == "protocolSection.designModule.phases"
        assert evidence.raw == ["PHASE1", "PHASE2"]


def test_study_without_phase_has_none_and_no_evidence(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    study = next(s for s in studies if "phases" not in s["protocolSection"]["designModule"])
    trial = normalize_study(study)
    assert trial.phase is None
    assert "phase" not in trial.evidence


# ---- country (explode + dedupe) -----------------------------------------------------------------


def test_countries_are_deduplicated_and_cite_the_first_site(load_fixture):
    study = load_fixture("placebo_arms")["studies"][0]
    locations = study["protocolSection"]["contactsLocationsModule"]["locations"]
    raw = [location["country"] for location in locations]
    assert len(raw) == 185

    trial = normalize_study(study)
    assert trial.country == list(dict.fromkeys(raw))
    assert len(trial.country) < len(raw)
    for evidence in trial.evidence["country"]:
        index = raw.index(evidence.raw)
        assert evidence.path == f"protocolSection.contactsLocationsModule.locations[{index}].country"


def test_study_without_locations_has_no_countries(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    without = [s for s in studies if not resolve_path(s, REGISTRY["country"].path)]
    assert len(without) == 5
    for study in without:
        trial = normalize_study(study)
        assert trial.country == [] and "country" not in trial.evidence


# ---- interventions and arms ---------------------------------------------------------------------


def test_placebo_trial_records_which_arm_types_each_intervention_is_in(load_fixture):
    trial = normalize_study(load_fixture("placebo_arms")["studies"][0])
    assert trial.nct_id == "NCT04003636"
    assert trial.intervention == ["Pembrolizumab", "Gemcitabine", "Cisplatin", "Placebo"]
    assert trial.intervention_type == ["Biological", "Drug"]  # labels for people...
    assert [e.raw for e in trial.evidence["intervention_type"]] == ["BIOLOGICAL", "DRUG"]  # ...raw in evidence
    arms = {record.name: record.arm_types for record in trial.intervention_records}
    assert arms == {
        "Pembrolizumab": ["EXPERIMENTAL"],
        "Gemcitabine": ["EXPERIMENTAL", "PLACEBO_COMPARATOR"],
        "Cisplatin": ["EXPERIMENTAL", "PLACEBO_COMPARATOR"],
        "Placebo": ["PLACEBO_COMPARATOR"],
    }
    assert {record.name: record.type for record in trial.intervention_records}["Placebo"] == "DRUG"


def test_arm_without_a_type_is_kept_as_none_not_dropped():
    study = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT00000001"},
            "armsInterventionsModule": {
                "armGroups": [{"label": "A"}, {"label": "B", "type": "PLACEBO_COMPARATOR"}],
                "interventions": [{"type": "DRUG", "name": "Saline", "armGroupLabels": ["A", "B", "C"]}],
            },
        }
    }
    [record] = normalize_study(study).intervention_records
    assert record.arm_types == [None, "PLACEBO_COMPARATOR", None]


# ---- dates and duration -------------------------------------------------------------------------


def test_dates_keep_the_api_precision(load_fixture):
    trial = normalize_study(load_fixture("placebo_arms")["studies"][0])
    assert (trial.start_year, trial.start_date, trial.completion_date) == (2019, "2019-09-24", "2025-04-01")
    assert trial.evidence["start_year"][0].raw == "2019-09-24"
    assert trial.duration_months == 66.2
    assert [e.path.rsplit(".", 2)[1] for e in trial.evidence["duration_months"]] == [
        "startDateStruct",
        "completionDateStruct",
    ]


def test_month_only_dates_appear_in_the_fixtures_and_give_a_year(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"] + load_fixture("lung_recruiting")["studies"]
    month_only = [
        s for s in studies if len(s["protocolSection"]["statusModule"]["startDateStruct"]["date"]) == 7
    ]
    assert month_only
    for study in month_only:
        raw = study["protocolSection"]["statusModule"]["startDateStruct"]["date"]
        trial = normalize_study(study)
        assert trial.start_year == int(raw[:4])
        assert trial.start_date == raw


@pytest.mark.parametrize(
    ("start", "end", "months"),
    [
        ("2020-01-01", "2021-01-01", 12.0),
        ("2020-01", "2021-07", 18.0),
        ("2020-01-31", "2020-03", 2.0),  # one side has no day: whole months only
        ("2020-05", "2020-05", 0.0),
        ("2021-01-01", "2020-01-01", None),  # completion before start
        (None, "2020-01-01", None),
        ("2020-01-01", None, None),
        ("2020", "2021-01", None),
        ("2020-13-01", "2021-01-01", None),
        ("soon", "2021-01-01", None),
    ],
)
def test_months_between(start, end, months):
    assert months_between(start, end) == months


def test_malformed_values_become_missing_instead_of_raising():
    study = {
        "hasResults": "yes",
        "protocolSection": {
            "identificationModule": {"nctId": "NCT00000002"},
            "statusModule": {"startDateStruct": {"date": "TBD"}, "overallStatus": "  "},
            "designModule": {"enrollmentInfo": {"count": "many"}, "phases": []},
            "conditionsModule": {"conditions": ["Asthma", " Asthma ", "", None, "COPD"]},
        },
    }
    trial = normalize_study(study)
    assert (trial.start_year, trial.start_date, trial.status, trial.enrollment) == (None, None, None, None)
    assert trial.has_results is None
    assert trial.phase is None
    assert trial.condition == ["Asthma", "COPD"]
    assert trial.duration_months is None


# ---- field selection ----------------------------------------------------------------------------


def test_only_requested_fields_are_filled(load_fixture):
    study = load_fixture("placebo_arms")["studies"][0]
    trial = normalize_study(study, ["phase"])
    assert set(trial.evidence) == {"nct_id", "phase"}
    assert trial.country == [] and trial.sponsor is None and trial.intervention_records == []


def test_requesting_a_derived_field_also_fills_its_sources(load_fixture):
    study = load_fixture("placebo_arms")["studies"][0]
    trial = normalize_study(study, ["duration_months"])
    assert set(trial.evidence) == {"nct_id", "start_year", "completion_date", "duration_months"}


def test_study_without_nct_id_is_an_error():
    with pytest.raises(ValueError):
        normalize_study({"protocolSection": {}})


def test_unknown_field_name_is_an_error(load_fixture):
    with pytest.raises(KeyError):
        normalize_study(load_fixture("placebo_arms")["studies"][0], ["not_a_field"])


# ---- user-facing labels and date types ----------------------------------------------------------


@pytest.mark.parametrize("fixture", STUDY_FIXTURES)
def test_no_raw_api_code_reaches_a_categorical_value(load_fixture, fixture):
    for study in load_fixture(fixture)["studies"]:
        trial = normalize_study(study)
        shown = [trial.phase, trial.status, trial.sponsor_class, *trial.intervention_type]
        for value in shown:
            assert value is None or value == "NIH" or (value != value.upper() and "_" not in value), value


def test_status_is_labelled_and_its_evidence_stays_raw(load_fixture):
    trial = normalize_study(load_fixture("lung_recruiting")["studies"][0])
    assert trial.status == "Recruiting"
    assert trial.evidence["status"][0].raw == "RECRUITING"
    assert trial.sponsor_class in REGISTRY["sponsor_class"].labels.values()


@pytest.mark.parametrize(
    ("field", "raw", "label"),
    [
        ("status", "ACTIVE_NOT_RECRUITING", "Active, not recruiting"),
        ("status", "UNKNOWN", "Unknown status"),
        ("sponsor_class", "OTHER_GOV", "Other government"),
        ("intervention_type", "DIETARY_SUPPLEMENT", "Dietary supplement"),
        ("status", "BRAND_NEW_STATUS", "BRAND_NEW_STATUS"),  # unknown to us: shown as it came, not dropped
        ("sponsor", "Pfizer", "Pfizer"),
        ("status", None, None),
    ],
)
def test_display_label(field, raw, label):
    assert display_label(field, raw) == label


def test_date_types_are_read_from_the_record(load_fixture):
    trial = normalize_study(load_fixture("placebo_arms")["studies"][0])
    assert (trial.start_date_type, trial.completion_date_type) == ("ACTUAL", "ACTUAL")


@pytest.mark.parametrize("fixture", STUDY_FIXTURES)
def test_date_types_match_the_raw_json(load_fixture, fixture):
    for study in load_fixture(fixture)["studies"]:
        status = study["protocolSection"]["statusModule"]
        trial = normalize_study(study)
        assert trial.start_date_type == status.get("startDateStruct", {}).get("type")
        assert trial.completion_date_type == status.get("completionDateStruct", {}).get("type")


def test_date_type_is_not_read_when_the_date_was_not_asked_for(load_fixture):
    trial = normalize_study(load_fixture("placebo_arms")["studies"][0], ["phase"])
    assert trial.start_date_type is None and trial.completion_date_type is None
