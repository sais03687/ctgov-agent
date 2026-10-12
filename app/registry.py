"""TASK:     Lists each trial field that a plan can use, with all its rules.
GRADING:  System Design (to add a field is one entry). AI / Agent Design (the LLM cannot use other fields).
INPUT:    None. This file is a fixed list.
OUTPUT:   REGISTRY (field name -> FieldDef), and functions that read it.
USED BY:  The validator, the planner prompt, the registry client, normalization, the
          aggregators, and citations.
DIAGRAM:  5 (gate 2 examines each field name against this list).

PARTS:
    1. Display labels
    2. The list of fields
    3. Keys that match different spellings
    4. Search expressions for buckets
    5. Functions that read the field registry

To add a field: add one entry in part 2 and one attribute on `Trial` (app/models.py).
"""
import re
from collections.abc import Hashable, Iterable
from typing import Any

from app.models import (
    NO_LABEL,
    UNKNOWN_LABEL,
    YES_LABEL,
    FieldDef,
    FieldKind,
    InterventionType,
    MultiRule,
    OverallStatus,
    Phase,
    PlanFilters,
    Scalar,
    SponsorClass,
)


# ===== PART 1: Display labels =====================================================================
# WHAT: The text that the user sees for each raw registry code.
# WHY:  The user must not see a raw code such as ACTIVE_NOT_RECRUITING.


def _values(enum: type) -> tuple[str, ...]:
    """WHAT: Returns the values of an enum as a list of text."""
    return tuple(member.value for member in enum)


# Raw API value -> the label that users see. Users do not see a raw code.
PHASE_LABELS = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    "NA": "Not Applicable",
}
STATUS_LABELS = {
    "NOT_YET_RECRUITING": "Not yet recruiting",
    "RECRUITING": "Recruiting",
    "ENROLLING_BY_INVITATION": "Enrolling by invitation",
    "ACTIVE_NOT_RECRUITING": "Active, not recruiting",
    "COMPLETED": "Completed",
    "SUSPENDED": "Suspended",
    "TERMINATED": "Terminated",
    "WITHDRAWN": "Withdrawn",
    "AVAILABLE": "Available",
    "NO_LONGER_AVAILABLE": "No longer available",
    "TEMPORARILY_NOT_AVAILABLE": "Temporarily not available",
    "APPROVED_FOR_MARKETING": "Approved for marketing",
    "WITHHELD": "Withheld",
    "UNKNOWN": "Unknown status",  # ClinicalTrials.gov gives this status. It is not a missing value.
}
SPONSOR_CLASS_LABELS = {
    "NIH": "NIH",
    "FED": "Other U.S. federal agency",
    "OTHER_GOV": "Other government",
    "INDIV": "Individual",
    "INDUSTRY": "Industry",
    "NETWORK": "Network",
    "AMBIG": "Ambiguous",
    "OTHER": "Other (e.g. university, hospital)",
    "UNKNOWN": "Unknown",
}
INTERVENTION_TYPE_LABELS = {
    "DRUG": "Drug",
    "BIOLOGICAL": "Biological",
    "DEVICE": "Device",
    "PROCEDURE": "Procedure",
    "RADIATION": "Radiation",
    "BEHAVIORAL": "Behavioral",
    "GENETIC": "Genetic",
    "DIETARY_SUPPLEMENT": "Dietary supplement",
    "COMBINATION_PRODUCT": "Combination product",
    "DIAGNOSTIC_TEST": "Diagnostic test",
    "OTHER": "Other",
}


# ===== PART 2: The list of fields =================================================================
# WHAT: One FieldDef entry for each field: its path, kind, filter, labels, and order.
# WHY:  All facts about a field are in one place.

_FIELDS: tuple[FieldDef, ...] = (
    FieldDef(
        name="nct_id",
        display_name="NCT ID",
        kind=FieldKind.ID,
        api_field="NCTId",
        path="protocolSection.identificationModule.nctId",
    ),
    FieldDef(
        name="brief_title",
        display_name="Trial title",
        kind=FieldKind.TEXT,
        api_field="BriefTitle",
        path="protocolSection.identificationModule.briefTitle",
    ),
    FieldDef(
        name="phase",
        display_name="Phase",
        kind=FieldKind.CATEGORICAL,
        multi=MultiRule.COMBINE,
        api_field="Phase",
        path="protocolSection.designModule.phases",
        filter_param="AREA[Phase]",
        noun="phase",
        enum=_values(Phase),
        labels=PHASE_LABELS,
        # Live check: with these two, the sum of the phase buckets of 14,613 lung cancer trials
        # is exactly 14,613.
        combinations={"Phase 1/2": ("PHASE1", "PHASE2"), "Phase 2/3": ("PHASE2", "PHASE3")},
        order=(
            "Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4",
            "Not Applicable",
        ),
    ),
    FieldDef(
        name="status",
        display_name="Status",
        kind=FieldKind.CATEGORICAL,
        api_field="OverallStatus",
        path="protocolSection.statusModule.overallStatus",
        filter_param="filter.overallStatus",
        noun="status",
        enum=_values(OverallStatus),
        labels=STATUS_LABELS,
        order=tuple(STATUS_LABELS.values()),
    ),
    FieldDef(
        name="start_year",
        display_name="Start year",
        kind=FieldKind.TIME,
        api_field="StartDate",
        path="protocolSection.statusModule.startDateStruct.date",
        filter_param="AREA[StartDate]",
        extra_api_fields=("StartDateType",),  # ACTUAL or ESTIMATED
    ),
    FieldDef(
        name="start_date",
        display_name="Start date",
        kind=FieldKind.DATE,
        api_field="StartDate",
        path="protocolSection.statusModule.startDateStruct.date",
        extra_api_fields=("StartDateType",),
    ),
    FieldDef(
        name="completion_date",
        display_name="Completion date",
        kind=FieldKind.DATE,
        api_field="CompletionDate",
        path="protocolSection.statusModule.completionDateStruct.date",
        extra_api_fields=("CompletionDateType",),
    ),
    FieldDef(
        name="last_updated",
        display_name="Last updated",
        kind=FieldKind.DATE,
        api_field="LastUpdatePostDate",
        path="protocolSection.statusModule.lastUpdatePostDateStruct.date",
    ),
    FieldDef(
        name="country",
        display_name="Country",
        kind=FieldKind.CATEGORICAL,
        multi=MultiRule.EXPLODE,
        api_field="LocationCountry",
        path="protocolSection.contactsLocationsModule.locations[].country",
        # This uses the country field, not `query.locn`. `query.locn` also searches city and state
        # text. There, "Georgia" found 221 melanoma trials (mostly the US state). The country has 12.
        # Live check: the field search also accepts "USA", "UK", and "Korea".
        filter_param="AREA[LocationCountry]",
        noun="country",
    ),
    FieldDef(
        name="condition",
        display_name="Condition",
        kind=FieldKind.CATEGORICAL,
        multi=MultiRule.EXPLODE,
        api_field="Condition",
        path="protocolSection.conditionsModule.conditions[]",
        filter_param="query.cond",
        noun="condition",
        # Sponsors write the same condition in different ways. The trials of one drug had "Non Small
        # Cell Lung Cancer" (49), "Non-small Cell Lung Cancer" (41), "Non-Small Cell Lung Cancer" (39).
        merge_spellings="name",
    ),
    FieldDef(
        name="intervention",
        display_name="Intervention",
        kind=FieldKind.CATEGORICAL,
        multi=MultiRule.EXPLODE,
        api_field="InterventionName",
        path="protocolSection.armsInterventionsModule.interventions[].name",
        filter_param="query.intr",
        noun="drug",
        clean_as_drug=True,
        merge_spellings="name",
        # A placebo has the type DRUG. To identify it, the code needs the arms that give it.
        extra_api_fields=(
            "InterventionType",
            "InterventionArmGroupLabel",
            "ArmGroupLabel",
            "ArmGroupType",
        ),
    ),
    FieldDef(
        name="intervention_type",
        display_name="Intervention type",
        kind=FieldKind.CATEGORICAL,
        multi=MultiRule.EXPLODE,
        api_field="InterventionType",
        path="protocolSection.armsInterventionsModule.interventions[].type",
        filter_param="AREA[InterventionType]",
        noun="intervention type",
        enum=_values(InterventionType),
        labels=INTERVENTION_TYPE_LABELS,
    ),
    FieldDef(
        name="sponsor",
        display_name="Lead sponsor",
        kind=FieldKind.CATEGORICAL,
        api_field="LeadSponsorName",
        path="protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
        filter_param="query.spons",
        noun="sponsor",
        merge_spellings="company",
        # Live measurement on 11 sponsor bars: a phrase search gave each bar exactly. A full-match
        # search did not find some trials that have the same name in the record (4 bars wrong).
        full_match_search=False,
    ),
    FieldDef(
        name="sponsor_class",
        display_name="Sponsor category",
        kind=FieldKind.CATEGORICAL,
        api_field="LeadSponsorClass",
        path="protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
        filter_param="AREA[LeadSponsorClass]",
        noun="sponsor category",
        enum=_values(SponsorClass),
        labels=SPONSOR_CLASS_LABELS,
        order=tuple(SPONSOR_CLASS_LABELS.values()),
    ),
    FieldDef(
        name="enrollment",
        display_name="Enrollment",
        kind=FieldKind.NUMERIC,
        api_field="EnrollmentCount",
        path="protocolSection.designModule.enrollmentInfo.count",
        filter_param="AREA[EnrollmentCount]",
        unit="participants",
    ),
    FieldDef(
        name="duration_months",
        display_name="Duration",
        kind=FieldKind.NUMERIC,
        unit="months",
        derived_from=("start_year", "completion_date"),
    ),
    FieldDef(
        name="has_results",
        display_name="Results posted",
        kind=FieldKind.BOOLEAN,
        api_field="HasResults",
        path="hasResults",
        filter_param="AREA[HasResults]",
    ),
)

REGISTRY: dict[str, FieldDef] = {field.name: field for field in _FIELDS}

# Plan filter key -> the field that it filters.
FILTER_FIELDS: dict[str, str] = {
    "condition": "condition",
    "drug": "intervention",
    "sponsor": "sponsor",
    "country": "country",
    "status": "status",
    "phase": "phase",
    "start_year_min": "start_year",
    "start_year_max": "start_year",
}
assert set(FILTER_FIELDS) == set(PlanFilters.model_fields), "FILTER_FIELDS out of sync with PlanFilters"


def get_field(name: str) -> FieldDef | None:
    """WHAT: Returns the FieldDef of a field name. Raises KeyError for a name that is not known."""
    return REGISTRY.get(name)


# ===== PART 3: Keys that match different spellings ================================================
# WHAT: Rules that decide if two spellings are the same name.
# WHY:  One name written in two ways must be one bar, not two.

_COMPANY_SUFFIXES = {"inc", "incorporated", "ltd", "limited", "llc", "corp", "corporation"}
_HYPHENS = re.compile(r"[-‐‑‒–—]")


def name_key(text: str) -> str:
    """WHAT:   Returns the key that matches two spellings of a free-text name.
    WHY:    "Non-small Cell" and "Non Small Cell" must be the same bucket.
    INPUT:  text - the name.
    OUTPUT: The name with no capital letters, hyphens, or extra spaces.

    The key does no more than that. "NSCLC" and "Non-Small Cell Lung Cancer" stay separate.
    """
    return " ".join(_HYPHENS.sub(" ", text.casefold()).split())


def sponsor_key(name: str) -> str:
    """WHAT:   Returns the key that matches two spellings of a sponsor name.
    WHY:    "Pfizer Inc." and "Pfizer" are the same sponsor.
    INPUT:  name - the sponsor name.
    OUTPUT: The name in lowercase, with no punctuation and no Inc./Ltd/LLC/Corp. at the end.
    """
    words = re.sub(r"[^\w\s]", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


_KEY_RULES = {"name": name_key, "company": sponsor_key}


def match_key(name: str, value: Any) -> Hashable:
    """WHAT:   Returns the part of a value that two values must share to be in the same bucket.
    INPUT:  name - the field name. value - the value.
    OUTPUT: The value itself, or its spelling key for a field with merged spellings.
    """
    rule = REGISTRY[name].merge_spellings
    if rule is None or not isinstance(value, str):
        return value
    return _KEY_RULES[rule](value) or value


# ===== PART 4: Search expressions for buckets =====================================================
# WHAT: Makes the registry search for the trials of one bucket.
# WHY:  The exact counts and the citation links use the same search, so they cannot disagree.


def bucket_labels(name: str) -> list[str]:
    """WHAT: Returns each possible bucket of a fixed-value field, with the labels that normalization gives."""
    field = REGISTRY[name]
    return [*field.labels.values(), *(field.combinations or {}), UNKNOWN_LABEL]


def bucket_expression(name: str, label: Scalar, raw_values: Iterable[str] = ()) -> str | None:
    """WHAT:   Returns the search expression that selects exactly the trials of one bucket.
    WHY:    The exact bucket counts (tier 2) and the citation links use this one function.
            Thus the count and the link cannot disagree.
    INPUT:  name - the field name. label - the bucket label.
            raw_values - the original spellings of a free-text bucket (optional).
    OUTPUT: The expression, or None when the API cannot express the bucket.
    """
    field = REGISTRY[name]
    if field.api_field is None:
        return None
    area = f"AREA[{field.api_field}]"
    if label == UNKNOWN_LABEL:
        return f"{area}MISSING"
    if field.kind is FieldKind.TIME:
        return f"{area}RANGE[{label}-01-01,{label}-12-31]"
    if field.kind is FieldKind.BOOLEAN:
        return f"{area}{'true' if label == YES_LABEL else 'false'}" if label in (YES_LABEL, NO_LABEL) else None
    if field.kind is not FieldKind.CATEGORICAL:
        return None
    if field.combinations and label in field.combinations:
        return " AND ".join(f"{area}{value}" for value in field.combinations[label])
    if field.labels:
        raw = next((value for value, shown in field.labels.items() if shown == label), None)
        if raw is None:
            return None
        if field.multi is not MultiRule.COMBINE:
            return f"{area}{raw}"
        # A combine bucket holds trials with this value only ("Phase 1" does not include Phase 1/2).
        others = " OR ".join(f"{area}{other}" for other in field.labels if other != raw)
        return f"{area}{raw} AND NOT ({others})"
    # One spelling for each merged name is sufficient for the search, which ignores the same
    # differences. The shortest spelling is kept, because a phrase search for it also finds the longer ones.
    by_key: dict[Hashable, str] = {}
    for value in sorted((v.replace('"', " ").strip() for v in raw_values), key=len):
        by_key.setdefault(match_key(name, value), value)
    spellings = list(by_key.values()) or [str(label)]
    coverage = "COVERAGE[FullMatch]" if field.full_match_search else ""
    matches = [f'{area}{coverage}"{spelling}"' for spelling in spellings if spelling]
    return "(" + " OR ".join(matches) + ")" if len(matches) > 1 else (matches[0] if matches else None)


def range_expression(name: str, low: float, high: float | None) -> str | None:
    """WHAT:   Returns the search expression for whole numbers from `low` to `high`, inclusive.
    INPUT:  name - the field name. low, high - the limits. `high` = None means no upper limit.
    OUTPUT: The expression, or None when the field cannot be searched.
    """
    field = REGISTRY[name]
    bounds = [low] if high is None else [low, high]
    if field.api_field is None or any(float(bound) != int(bound) for bound in bounds):
        return None
    upper = "MAX" if high is None else str(int(high))
    return f"AREA[{field.api_field}]RANGE[{int(low)},{upper}]"


def bucket_search_is_exact(name: str) -> bool:
    """WHAT:   Returns True if `bucket_expression` selects exactly the trials of the bucket.
    OUTPUT: True for fixed codes, years, yes/no values, and the bucket for missing values.
            False for free-text names (the API matches them loosely) and for number bins.
    """
    field = REGISTRY[name]
    return bool(field.labels) or field.kind in (FieldKind.TIME, FieldKind.BOOLEAN)


# ===== PART 5: Functions that read the field registry =============================================
# WHAT: Small functions that other files use to read a field.
# WHY:  Other files do not need to know the structure of the list.


def row_key(name: str) -> str:
    """WHAT: Returns the key of the bucket of a field in a data row. Time buckets use the key for years."""
    return "year" if REGISTRY[name].kind is FieldKind.TIME else name


def field_names(*kinds: FieldKind) -> list[str]:
    """WHAT: Returns the field names. Optionally, only the names of the given kinds."""
    return [f.name for f in REGISTRY.values() if not kinds or f.kind in kinds]


def api_fields_for(names: Iterable[str]) -> list[str]:
    """WHAT:   Returns the short `fields=` names that the API needs for the given fields.
    INPUT:  names - the field names.
    OUTPUT: A list of API field names. NCTId is first.
    """
    wanted = ["NCTId"]

    def add(name: str) -> None:
        field = REGISTRY[name]
        for dependency in field.derived_from:
            add(dependency)
        for api_field in (field.api_field, *field.extra_api_fields):
            if api_field and api_field not in wanted:
                wanted.append(api_field)

    for name in names:
        add(name)
    return wanted


def resolve_path(study: dict[str, Any], path: str) -> list[tuple[str, Any]]:
    """WHAT:   Returns each value at a path in one study, with its indexed path.
    WHY:    Citations quote the indexed path, for example `a.b[3].c`.
    INPUT:  study - the raw study record. path - the path. `a.b[].c` goes through the list at `a.b`.
    OUTPUT: A list of (indexed path, value). A missing key gives no value and no error.
    """
    found: list[tuple[str, Any]] = [("", study)]
    for segment in path.split("."):
        key, iterate = (segment[:-2], True) if segment.endswith("[]") else (segment, False)
        next_found: list[tuple[str, Any]] = []
        for prefix, node in found:
            if not isinstance(node, dict) or node.get(key) is None:
                continue
            here = f"{prefix}.{key}" if prefix else key
            value = node[key]
            if not iterate:
                next_found.append((here, value))
            elif isinstance(value, list):
                next_found.extend((f"{here}[{i}]", item) for i, item in enumerate(value))
        found = next_found
    return found
