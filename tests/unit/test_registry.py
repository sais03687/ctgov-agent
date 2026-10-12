"""TASK: Tests that the field registry agrees with real API responses.
GRADING: System Design. Code Quality.
"""
import pytest

from app.models import FieldKind, MultiRule, Trial
from app.registry import FILTER_FIELDS, REGISTRY, api_fields_for, field_names, resolve_path

STUDY_FIXTURES = ["pembrolizumab_page", "phase12", "placebo_arms", "lung_recruiting"]
# The 15 fields of spec section 6, plus `start_date` (added in Phase 6 with the author's
# approval, so a per-trial timeline has a real start-date field instead of borrowing start_year).
# and `brief_title` (also approved in Phase 6, to label timeline bars).
SPEC_FIELDS = [
    "nct_id", "brief_title", "phase", "status", "start_year", "start_date", "completion_date", "last_updated", "country",
    "condition", "intervention", "intervention_type", "sponsor", "sponsor_class", "enrollment",
    "duration_months", "has_results",
]


def test_registry_has_every_field_in_the_spec_table():
    assert list(REGISTRY) == SPEC_FIELDS


@pytest.mark.parametrize("name", [n for n, f in REGISTRY.items() if f.path])
def test_every_path_resolves_on_pembrolizumab_page(load_fixture, name):
    studies = load_fixture("pembrolizumab_page")["studies"]
    path = REGISTRY[name].path
    assert any(resolve_path(study, path) for study in studies), f"{path} matched no study"


@pytest.mark.parametrize("fixture", STUDY_FIXTURES)
def test_nct_id_resolves_on_every_study(load_fixture, fixture):
    for study in load_fixture(fixture)["studies"]:
        [(path, value)] = resolve_path(study, REGISTRY["nct_id"].path)
        assert path == "protocolSection.identificationModule.nctId"
        assert value.startswith("NCT")


def test_derived_fields_have_no_path_and_resolvable_sources():
    derived = [f for f in REGISTRY.values() if f.path is None]
    assert [f.name for f in derived] == ["duration_months"]
    for field in derived:
        assert field.derived_from and field.api_field is None
        assert all(REGISTRY[source].path for source in field.derived_from)


@pytest.mark.parametrize("fixture", STUDY_FIXTURES)
@pytest.mark.parametrize("name", [n for n, f in REGISTRY.items() if f.enum])
def test_enum_lists_cover_every_value_in_the_fixtures(load_fixture, fixture, name):
    field = REGISTRY[name]
    for study in load_fixture(fixture)["studies"]:
        for path, value in resolve_path(study, field.path):
            values = value if isinstance(value, list) else [value]
            assert set(values) <= set(field.enum), f"{path}: {values} not in the {name} enum"


def test_explode_path_returns_one_indexed_entry_per_list_item(load_fixture):
    study = load_fixture("placebo_arms")["studies"][0]
    names = resolve_path(study, REGISTRY["intervention"].path)
    assert names == [
        ("protocolSection.armsInterventionsModule.interventions[0].name", "Pembrolizumab"),
        ("protocolSection.armsInterventionsModule.interventions[1].name", "Gemcitabine"),
        ("protocolSection.armsInterventionsModule.interventions[2].name", "Cisplatin"),
        ("protocolSection.armsInterventionsModule.interventions[3].name", "Placebo"),
    ]


def test_explode_path_on_a_list_of_scalars(load_fixture):
    study = load_fixture("placebo_arms")["studies"][0]
    conditions = resolve_path(study, REGISTRY["condition"].path)
    assert len(conditions) == len(study["protocolSection"]["conditionsModule"]["conditions"])
    assert conditions[0][0] == "protocolSection.conditionsModule.conditions[0]"


def test_combine_path_returns_the_whole_list_once(load_fixture):
    studies = load_fixture("phase12")["studies"]
    for study in studies:
        [(path, raw)] = resolve_path(study, REGISTRY["phase"].path)
        assert path == "protocolSection.designModule.phases"
        assert raw == ["PHASE1", "PHASE2"]


def test_missing_path_yields_nothing_instead_of_raising(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    without_phase = [s for s in studies if "phases" not in s["protocolSection"]["designModule"]]
    assert without_phase, "fixture should contain a study with no phase"
    assert resolve_path(without_phase[0], REGISTRY["phase"].path) == []
    assert resolve_path({}, REGISTRY["country"].path) == []


def test_multi_rules_match_the_spec_table():
    rules = {name: field.multi for name, field in REGISTRY.items()}
    assert rules["phase"] is MultiRule.COMBINE
    explode = {n for n, rule in rules.items() if rule is MultiRule.EXPLODE}
    assert explode == {"country", "condition", "intervention", "intervention_type"}
    # an explode field must iterate a list, and only an explode field may
    for name, field in REGISTRY.items():
        assert ("[]" in (field.path or "")) == (field.multi is MultiRule.EXPLODE), name


def test_every_registry_field_is_a_trial_attribute():
    assert set(REGISTRY) <= set(Trial.model_fields)


def test_every_plan_filter_maps_to_a_filterable_field():
    for filter_key, field_name in FILTER_FIELDS.items():
        assert REGISTRY[field_name].filter_param, f"{filter_key} -> {field_name} cannot be filtered"


def test_tier2_fields_have_an_enum_and_a_filter():
    for name in ["phase", "status", "sponsor_class", "intervention_type"]:
        assert REGISTRY[name].enum and REGISTRY[name].filter_param


def test_field_names_filters_by_kind():
    assert field_names(FieldKind.NUMERIC) == ["enrollment", "duration_months"]
    assert field_names() == SPEC_FIELDS


def test_api_fields_for_requests_only_what_the_plan_needs():
    assert api_fields_for(["phase"]) == ["NCTId", "Phase"]
    assert api_fields_for(["duration_months"]) == [
        "NCTId", "StartDate", "StartDateType", "CompletionDate", "CompletionDateType",
    ]
    assert api_fields_for(["intervention"]) == [
        "NCTId", "InterventionName", "InterventionType", "InterventionArmGroupLabel",
        "ArmGroupLabel", "ArmGroupType",
    ]
    with pytest.raises(KeyError):
        api_fields_for(["not_a_field"])


def test_api_field_names_are_the_ones_the_fixtures_were_fetched_with():
    fetched = {
        "NCTId", "BriefTitle", "Phase", "OverallStatus", "StartDate", "StartDateType", "CompletionDate",
        "CompletionDateType", "LastUpdatePostDate", "LeadSponsorName", "LeadSponsorClass", "EnrollmentCount", "LocationCountry", "Condition",
        "InterventionName", "InterventionType", "InterventionArmGroupLabel", "ArmGroupLabel",
        "ArmGroupType", "HasResults",
    }
    assert set(api_fields_for(REGISTRY)) == fetched


# ---- user-facing labels -------------------------------------------------------------------------


@pytest.mark.parametrize("name", [n for n, f in REGISTRY.items() if f.enum])
def test_every_fixed_value_has_a_user_facing_label(name):
    field = REGISTRY[name]
    assert set(field.labels) == set(field.enum)
    for raw, label in field.labels.items():
        assert label and "_" not in label and label != label.upper() or label == "NIH", (raw, label)


@pytest.mark.parametrize("name", [n for n, f in REGISTRY.items() if f.labels])
def test_labels_within_a_field_are_distinct(name):
    labels = list(REGISTRY[name].labels.values())
    assert len(labels) == len(set(labels))


def test_a_status_of_unknown_is_not_confused_with_a_missing_value():
    from app.models import UNKNOWN_LABEL

    assert REGISTRY["status"].labels["UNKNOWN"] == "Unknown status" != UNKNOWN_LABEL


def test_natural_order_uses_the_labels_people_see():
    for name in ["phase", "status", "sponsor_class"]:
        field = REGISTRY[name]
        assert set(field.labels.values()) <= set(field.order)
