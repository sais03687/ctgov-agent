"""TASK:     Defines all data types: request, plan, trial, chart, response.
GRADING:  Input/Output Design. The JSON Schema in GET /schema comes from these types.
INPUT:    None. This file has definitions only.
OUTPUT:   Pydantic models. A type is defined here one time, and no other file defines it again.
USED BY:  All files.

PARTS (the same sequence as the pipeline):
    1. Enums                   Fixed lists: registry codes, plan modes, chart types.
    2. The field type          FieldDef.
    3. The request             QueryRequest.
    4. The plan                QueryPlan, ValidationIssue.
    5. The data                Trial, FieldEvidence, FetchResult.
    6. Aggregation             AggregateResult and the network parts.
    7. Citations               CitationSet.
    8. Visualization           One model for each chart type.
    9. Run state and response  RunState, VisualizationResponse.

Note: the comment of each model class is also its description in the JSON Schema. Thus these
comments are plain sentences and do not use the WHAT / WHY labels.
"""
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ===== PART 1: Enums ==============================================================================
# WHAT: Fixed lists: registry codes, plan modes, chart types.
# WHY:  A value that is not in a list is refused.


class Phase(StrEnum):
    """The phase values of ClinicalTrials.gov. A trial can have two (PHASE1 + PHASE2 = "Phase 1/2")."""

    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"


class OverallStatus(StrEnum):
    """The recruitment status values of ClinicalTrials.gov."""

    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"
    WITHHELD = "WITHHELD"
    UNKNOWN = "UNKNOWN"


class SponsorClass(StrEnum):
    """The lead sponsor categories of ClinicalTrials.gov."""

    NIH = "NIH"
    FED = "FED"
    OTHER_GOV = "OTHER_GOV"
    INDIV = "INDIV"
    INDUSTRY = "INDUSTRY"
    NETWORK = "NETWORK"
    AMBIG = "AMBIG"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class InterventionType(StrEnum):
    """The intervention types of ClinicalTrials.gov. Note: a placebo usually has the type DRUG."""

    DRUG = "DRUG"
    BIOLOGICAL = "BIOLOGICAL"
    DEVICE = "DEVICE"
    PROCEDURE = "PROCEDURE"
    RADIATION = "RADIATION"
    BEHAVIORAL = "BEHAVIORAL"
    GENETIC = "GENETIC"
    DIETARY_SUPPLEMENT = "DIETARY_SUPPLEMENT"
    COMBINATION_PRODUCT = "COMBINATION_PRODUCT"
    DIAGNOSTIC_TEST = "DIAGNOSTIC_TEST"
    OTHER = "OTHER"


class ArmGroupType(StrEnum):
    """The arm types of ClinicalTrials.gov. The placebo rule (network layer 1) uses them."""

    EXPERIMENTAL = "EXPERIMENTAL"
    ACTIVE_COMPARATOR = "ACTIVE_COMPARATOR"
    PLACEBO_COMPARATOR = "PLACEBO_COMPARATOR"
    SHAM_COMPARATOR = "SHAM_COMPARATOR"
    NO_INTERVENTION = "NO_INTERVENTION"
    OTHER = "OTHER"


class PlanMode(StrEnum):
    """How trials become data points. There is one aggregation module for each mode."""

    AGGREGATE = "aggregate"  # Count trials for each bucket of 1 or 2 group_by fields.
    HISTOGRAM = "histogram"  # Count trials for each number bin.
    POINTS = "points"  # One datum for each trial (scatter, gantt).
    NETWORK = "network"  # Entities are nodes. Shared trials are edges.


class ChartType(StrEnum):
    BAR_CHART = "bar_chart"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    STACKED_BAR_CHART = "stacked_bar_chart"
    HEATMAP = "heatmap"
    TIME_SERIES = "time_series"
    HISTOGRAM = "histogram"
    SCATTER_PLOT = "scatter_plot"
    GANTT = "gantt"
    NETWORK_GRAPH = "network_graph"


class SortOrder(StrEnum):
    DESC = "desc"  # The largest count is first.
    ASC = "asc"  # The smallest count is first.
    NATURAL = "natural"  # The natural order of the field (years go up, phases go from early to late).


class FieldKind(StrEnum):
    """The uses that a field permits."""

    ID = "id"
    CATEGORICAL = "categorical"
    NUMERIC = "numeric"
    TIME = "time"  # A year bucket. A plan can group by it. Empty years get zero.
    DATE = "date"  # A calendar date as text: `YYYY-MM` or `YYYY-MM-DD`.
    BOOLEAN = "boolean"
    TEXT = "text"  # Free text shown as a label. A plan cannot group, bin, or filter by it.


class MultiRule(StrEnum):
    """The rule for a trial that has two or more values for a field."""

    SINGLE = "single"  # A maximum of one value for each trial.
    EXPLODE = "explode"  # Count the trial one time for each different value.
    COMBINE = "combine"  # Join the values into one label ("Phase 1/2").


class ChannelType(StrEnum):
    """The measurement type of an encoding channel. A renderer uses it to select a scale."""

    CATEGORICAL = "categorical"
    QUANTITATIVE = "quantitative"
    TEMPORAL = "temporal"


class ResponseStatus(StrEnum):
    OK = "ok"
    UNSUPPORTED = "unsupported"  # The question is clear, but this data cannot answer it.
    ERROR = "error"  # An external service failed.


class InterventionLabel(StrEnum):
    """The fixed list of labels for the label model (network layer 4).

    The label tells what a name IS. It does not tell the function of the item in a trial.
    Dexamethasone given as supportive care is still a specific drug.
    """

    SPECIFIC_DRUG = "specific_drug"  # A named drug, biological, vaccine, or cell therapy.
    PLACEBO_OR_SHAM = "placebo_or_sham"
    UNSPECIFIC_CARE = "unspecific_care"  # "chemotherapy", "standard of care", "investigator's choice"
    NOT_A_DRUG = "not_a_drug"  # A procedure, device, test, behavior, radiation, or questionnaire.


# ===== PART 2: The field type =====================================================================
# WHAT: FieldDef: all facts about one field.
# WHY:  app/registry.py holds one FieldDef for each field.


class FieldDef(BaseModel):
    """One field that a plan can use. `app/registry.py` holds the entries."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Name used in plans, rows and `Trial` attributes.")
    display_name: str = Field(description="Human label for axis titles and notes.")
    kind: FieldKind
    multi: MultiRule = MultiRule.SINGLE
    api_field: str | None = Field(
        default=None, description="Short name for the API `fields=` parameter. None if derived."
    )
    path: str | None = Field(
        default=None,
        description="Dotted path in the study JSON; `[]` iterates a list. None if derived.",
    )
    filter_param: str | None = Field(
        default=None,
        description="API parameter that filters on this field: a query parameter name such as "
        "`query.cond`, or `AREA[Name]` for `filter.advanced`. None if not filterable.",
    )
    enum: tuple[str, ...] | None = Field(
        default=None, description="Allowed raw API values, when the field has a fixed set."
    )
    unit: str | None = Field(default=None, description="Unit for numeric fields.")
    labels: dict[str, str] | None = Field(
        default=None,
        description="Raw API value -> the label shown to users (`ACTIVE_NOT_RECRUITING` -> "
        "`Active, not recruiting`). Raw values stay in citations and in filter inputs.",
    )
    combinations: dict[str, tuple[str, ...]] | None = Field(
        default=None,
        description="`combine` fields: label -> the raw values a trial really carries together "
        '("Phase 1/2" -> PHASE1 + PHASE2).',
    )
    merge_spellings: Literal["name", "company"] | None = Field(
        default=None,
        description="Free-text fields: count spellings of one name together. `name` ignores "
        "capital letters and hyphens; `company` also ignores punctuation and a trailing "
        "Inc./Ltd/LLC/Corp. None counts each distinct text separately.",
    )
    noun: str | None = Field(
        default=None, description="What one value is called in a sentence (`sponsor`, `drug`)."
    )
    clean_as_drug: bool = Field(
        default=False,
        description="In a network, keep only real drugs: placebos, shams, non-drug intervention "
        "types and unspecific names are removed (spec 8.4).",
    )
    full_match_search: bool = Field(
        default=True,
        description="Free-text fields: search for the whole value (`COVERAGE[FullMatch]`) rather "
        "than a phrase inside it. Off where the API's full-match index proved unreliable.",
    )
    order: tuple[str, ...] | None = Field(
        default=None,
        description="Natural order of the bucket labels (early phase before late). None means "
        "alphabetical, or numeric for time and numeric fields.",
    )
    derived_from: tuple[str, ...] = Field(
        default=(), description="Registry fields this one is computed from."
    )
    extra_api_fields: tuple[str, ...] = Field(
        default=(),
        description="Other API short names fetched alongside, needed to interpret this field.",
    )


# ===== PART 3: The request ========================================================================
# WHAT: QueryRequest: the body of POST /query.
# WHY:  The assignment asks for a documented request schema.

MAX_NAMES = 5
MIN_YEAR = 1990
YEARS_AHEAD = 5


def _clean_names(value: str | list[str] | None) -> list[str] | None:
    """Changes a name or a list of names into a list of 1 to 5 names, with no empty names and no duplicates."""
    if value is None:
        return None
    names: list[str] = []
    for raw in [value] if isinstance(value, str) else value:
        name = raw.strip()
        if not name:
            raise ValueError("names must not be empty")
        if name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    if not names:
        raise ValueError("give at least one name, or omit the field")
    if len(names) > MAX_NAMES:
        raise ValueError(f"at most {MAX_NAMES} names are allowed, got {len(names)}")
    return names


class QueryRequest(BaseModel):
    """The body of `POST /query`. Only `query` is mandatory.

    The optional fields are explicit filters. They have priority over the values that the
    planner reads from `query`. Each conflict gives a note in `meta.notes`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(
        min_length=3, max_length=500, description="The question, in natural language."
    )
    drug_name: str | list[str] | None = Field(
        default=None,
        description="Drug or intervention name(s), at most 5. More than one name makes one "
        "comparison series per name. Always a list after validation.",
    )
    condition: str | list[str] | None = Field(
        default=None,
        description="Condition or disease name(s), at most 5. More than one name makes one "
        "comparison series per name. Always a list after validation.",
    )
    sponsor: str | None = Field(default=None, min_length=1, description="Lead sponsor name.")
    trial_phase: list[Phase] | None = Field(
        default=None, min_length=1, description="Keep only trials in any of these phases."
    )
    status: list[OverallStatus] | None = Field(
        default=None, min_length=1, description="Keep only trials in any of these statuses."
    )
    country: str | None = Field(
        default=None, min_length=1, description="Keep only trials with a site in this country."
    )
    start_year: int | None = Field(
        default=None,
        description=f"Earliest trial start year, {MIN_YEAR} to current year + {YEARS_AHEAD}.",
    )
    end_year: int | None = Field(
        default=None,
        description=f"Latest trial start year, {MIN_YEAR} to current year + {YEARS_AHEAD}. "
        "Must not be before `start_year`.",
    )
    chart_type: ChartType | None = Field(
        default=None,
        description="Preferred chart. A suggestion: it is replaced, with a note, when it does "
        "not fit the data.",
    )
    max_citations: int = Field(
        default=10, ge=0, le=50, description="Most trial references listed per datum."
    )
    debug: bool = Field(default=True, description="Include the step trace in `meta.trace`.")

    @field_validator("drug_name", "condition")
    @classmethod
    def _names(cls, value: str | list[str] | None) -> list[str] | None:
        return _clean_names(value)

    @field_validator("start_year", "end_year")
    @classmethod
    def _year_in_range(cls, value: int | None) -> int | None:
        latest = date.today().year + YEARS_AHEAD
        if value is not None and not MIN_YEAR <= value <= latest:
            raise ValueError(f"year must be between {MIN_YEAR} and {latest}")
        return value

    @model_validator(mode="after")
    def _years_ordered(self) -> "QueryRequest":
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must not be after end_year")
        return self


# ===== PART 4: The plan ===========================================================================
# WHAT: QueryPlan: the only output of the LLM.
# WHY:  The LLM can change the result only through this structure.


class PlanFilters(BaseModel):
    """The filters that the user asked for. Each filter goes to the API. The service does not invent a filter (P4)."""

    model_config = ConfigDict(extra="forbid")

    condition: str | None = None
    drug: str | None = None
    sponsor: str | None = None
    country: str | None = None
    status: list[OverallStatus] | None = None
    phase: list[Phase] | None = None
    start_year_min: int | None = None
    start_year_max: int | None = None


class Series(BaseModel):
    """One group that the USER named, for comparison with the other groups. Each series has its own fetch."""

    model_config = ConfigDict(extra="forbid")

    label: str
    filters: PlanFilters = Field(default_factory=PlanFilters)


class BinSpec(BaseModel):
    """Bins of equal width for a number field."""

    model_config = ConfigDict(extra="forbid")

    field: str
    bin_width: float


class NetworkSpec(BaseModel):
    """The entities that are nodes. One field two times means co-occurrence in a trial."""

    model_config = ConfigDict(extra="forbid")

    source_field: str
    target_field: str
    max_edges: int = 60


class SearchTerms(BaseModel):
    """The search terms that are different from the words of the user.

    Examples: a corrected spelling, or a generic name for a brand name. The keys are fixed, so
    the schema stays closed.
    """

    model_config = ConfigDict(extra="forbid")

    drug: list[str] = Field(default_factory=list)
    condition: list[str] = Field(default_factory=list)
    sponsor: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    """A question written as data operations.

    This model examines the structure only. `app/validator.py` examines the field names, the
    mandatory keys for each mode, and the number limits. The validator reports all problems
    together as `ValidationIssue`s. It does not stop at the first problem.
    """

    model_config = ConfigDict(extra="forbid")

    mode: PlanMode
    filters: PlanFilters = Field(
        default_factory=PlanFilters, description="Applied to every series."
    )
    series: list[Series] = Field(
        default_factory=lambda: [Series(label="All")],
        description='1-5 groups the user named. No comparison = one series labelled "All".',
    )
    group_by: list[str] = Field(
        default_factory=list, description="aggregate mode: 1-2 registry fields to bucket by."
    )
    bin: BinSpec | None = Field(
        default=None, description="histogram mode, or a numeric group_by field."
    )
    x_field: str | None = Field(default=None, description="points mode (scatter): numeric field.")
    y_field: str | None = Field(default=None, description="points mode (scatter): numeric field.")
    start_field: str | None = Field(default=None, description="points mode (gantt): date field.")
    end_field: str | None = Field(default=None, description="points mode (gantt): date field.")
    network: NetworkSpec | None = Field(default=None, description="network mode.")
    sort: SortOrder = SortOrder.DESC
    limit: int = Field(default=10, description="Most buckets (or points) returned, 1-50.")
    chart_type: ChartType | None = Field(
        default=None, description="Suggested chart; code makes the final choice."
    )
    search_terms: SearchTerms = Field(
        default_factory=SearchTerms,
        description="Per filter, the term searched when it is not what the user typed.",
    )
    interpretation: str = Field(
        default="", description="One sentence restating the question as understood."
    )
    unsupported_reason: str | None = Field(
        default=None, description="Set when the question is vague or out of scope."
    )
    alternatives: list[str] = Field(
        default_factory=list,
        description="Only with unsupported_reason: up to three questions close to the user's that "
        "can be answered. Empty otherwise.",
    )


class ValidationIssue(BaseModel):
    """One problem of a plan. The words are suitable for the LLM and for a user."""

    path: str = Field(description="Where in the plan, e.g. `group_by[0]`.")
    message: str


class ToolCall(BaseModel):
    """One read-only tool call that the planner made. The trace keeps it."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    ms: float = 0.0


class PlanDraft(BaseModel):
    """The output of the planner: a plan if the output was a valid plan, and the steps that made it."""

    plan: QueryPlan | None = None
    parse_error: str | None = Field(default=None, description="Why the model's output was not a plan.")
    tool_calls: list[ToolCall] = Field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    conversation: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Every item exchanged with the model so far, so a retry can continue from it.",
    )


# ===== PART 5: The data ===========================================================================
# WHAT: Trial, FieldEvidence, and the fetch results.
# WHY:  Each value keeps its raw registry value for citations.


class FieldEvidence(BaseModel):
    """The exact raw API value of a normalized field, and its position in the study JSON."""

    path: str = Field(
        description="Indexed path, e.g. `protocolSection.contactsLocationsModule.locations[3].country`."
    )
    raw: Any = Field(description="The value exactly as the API returned it.")
    label: Any = Field(default=None, description="The normalized value this raw value became.")


class InterventionRecord(BaseModel):
    """One intervention of a trial, with the types of the arms that use it (for the placebo rule)."""

    name: str
    type: str | None = Field(default=None, description="Raw API intervention type, e.g. `DRUG`.")
    arm_types: list[str | None] = Field(
        default_factory=list,
        description="Type of each arm the intervention is given in; None where the arm has no type.",
    )


class Trial(BaseModel):
    """One study in a flat form. The attribute names are exactly the field names in the field registry.

    A field with the rule `explode` holds a list with no duplicates. All other fields hold
    one value, or None when the API has no value.
    """

    nct_id: str
    brief_title: str | None = None
    phase: str | None = Field(default=None, description='Display label, e.g. "Phase 1/2".')
    status: str | None = Field(default=None, description='Display label, e.g. "Recruiting".')
    start_year: int | None = None
    start_date: str | None = Field(default=None, description="`YYYY-MM` or `YYYY-MM-DD`.")
    start_date_type: str | None = Field(default=None, description="`ACTUAL` or `ESTIMATED` (planned).")
    completion_date: str | None = Field(default=None, description="`YYYY-MM` or `YYYY-MM-DD`.")
    completion_date_type: str | None = Field(default=None, description="`ACTUAL` or `ESTIMATED` (planned).")
    last_updated: str | None = Field(default=None, description="`YYYY-MM-DD`.")
    country: list[str] = Field(default_factory=list)
    condition: list[str] = Field(default_factory=list)
    intervention: list[str] = Field(default_factory=list)
    intervention_type: list[str] = Field(default_factory=list)
    sponsor: str | None = None
    sponsor_class: str | None = None
    enrollment: int | None = None
    duration_months: float | None = None
    has_results: bool | None = None

    intervention_records: list[InterventionRecord] = Field(default_factory=list)
    evidence: dict[str, list[FieldEvidence]] = Field(
        default_factory=dict,
        description="Registry field name -> raw evidence. One entry for single and combine "
        "fields; one per distinct value for explode fields.",
    )


# The label of the bucket for trials that have no value in a category field.
UNKNOWN_LABEL = "Unknown"
# The labels of the two buckets of a boolean field.
YES_LABEL, NO_LABEL = "Yes", "No"


class FetchErrorKind(StrEnum):
    OVER_CAP = "over_cap"  # Too many trials to read fully, and exact bucket counts are not possible.
    UNAVAILABLE = "unavailable"  # ClinicalTrials.gov failed after the retries.
    REJECTED = "rejected"  # ClinicalTrials.gov refused the request (HTTP 4xx).


class SeriesFetch(BaseModel):
    """The data that the client got for one series. The data is complete, or `error` is set (P6)."""

    label: str
    filters: PlanFilters = Field(description="Global and series filters merged.")
    api_params: dict[str, str] = Field(
        default_factory=dict, description="The filters as the API parameters that were sent."
    )
    trials_matched: int = Field(default=0, description="Exact `totalCount` from the API.")
    trials: list[Trial] = Field(default_factory=list)
    tier: Literal[1, 2] | None = Field(
        default=None, description="1 = every trial fetched; 2 = exact bucket counts + samples."
    )
    is_sample: bool = Field(default=False, description="True when `trials` is a tier-2 sample.")
    bucket_field: str | None = Field(
        default=None, description="Tier 2 only: the registry field the buckets belong to."
    )
    bucket_counts: dict[str, int] | None = Field(
        default=None,
        description="Tier 2 only: exact trial count per bucket, keyed by the same label "
        "normalization gives a trial in that bucket.",
    )
    error: str | None = Field(
        default=None, description="Plain-language reason this series could not be fetched."
    )
    error_kind: FetchErrorKind | None = None
    error_detail: str | None = Field(
        default=None, description="Technical cause, for the trace only (e.g. `HTTP 429, 3 attempts`)."
    )


class FetchResult(BaseModel):
    series: list[SeriesFetch]
    api_calls: int = 0
    cache_hits: int = 0
    data_timestamp: str | None = Field(
        default=None, description="When ClinicalTrials.gov last refreshed its data."
    )


# ===== PART 6: Aggregation ========================================================================
# WHAT: AggregateResult and the network parts.
# WHY:  All plan modes return the same result type.

Scalar = str | int | float | bool | None
DataRow = dict[str, Scalar]

# The fixed keys of a data row. Bucket keys are field names (`year` for a time field).
COUNT_KEY = "trial_count"
SERIES_KEY = "series"


class NetworkNode(BaseModel):
    id: str
    label: str
    type: str = Field(description="Registry field the entity comes from, e.g. `sponsor`.")
    trial_count: int


class NetworkEdge(BaseModel):
    id: str
    source: str = Field(description="`id` of a node.")
    target: str = Field(description="`id` of a node.")
    shared_trials: int


class NetworkData(BaseModel):
    nodes: list[NetworkNode] = Field(default_factory=list)
    edges: list[NetworkEdge] = Field(default_factory=list)


class PruningInfo(BaseModel):
    """The items removed from a network, and the meaning of a line, in plain language."""

    edges_shown: int
    edges_total: int
    min_shared_trials: int
    message: str


class BucketRef(BaseModel):
    """One fact that a datum tells about its trials: a field, and the bucket if there is one."""

    field: str
    bucket: Scalar = None


class DatumAbout(BaseModel):
    """The content of one datum. With it, citations are made the same way for all charts."""

    series: str = Field(description="Label of the series whose trials the datum counts.")
    label: str = Field(description="Human-readable name of the datum.")
    buckets: list[BucketRef] = Field(
        default_factory=list, description="The field(s) whose raw values are quoted as evidence."
    )
    search: list[str] | None = Field(
        default=None,
        description="Search expressions selecting the datum's trials. None: derive them from `buckets`.",
    )
    search_is_exact: bool | None = Field(
        default=None, description="Whether `search` selects exactly those trials. None: derive it."
    )


class AggregateResult(BaseModel):
    """The output of each aggregation mode: the data, and the trials for each datum."""

    mode: PlanMode
    rows: list[DataRow] = Field(
        default_factory=list, description="Flat rows, each with an `id`. Empty in network mode."
    )
    network: NetworkData | None = None
    contributing: dict[str, list[str]] = Field(
        default_factory=dict, description="Datum id -> NCT ids of the trials counted in it."
    )
    about: dict[str, DatumAbout] = Field(
        default_factory=dict, description="Datum id -> what the datum stands for."
    )
    exact_totals: dict[str, int] | None = Field(
        default=None,
        description="Tier 2 only: datum id -> exact count, where `contributing` is a sample.",
    )
    trials_used: int = 0
    dropped: dict[str, int] = Field(
        default_factory=dict, description="Reason -> number of trials left out."
    )
    notes: list[str] = Field(default_factory=list)
    pruning: PruningInfo | None = None
    llm_exchanges: list[dict[str, Any]] = Field(
        default_factory=list, description="Raw model input and output from this step, for the run log."
    )
    trace: dict[str, Any] = Field(
        default_factory=dict, description="Mode-specific details for the trace (not shown to users)."
    )


# ===== PART 7: Citations ==========================================================================
# WHAT: CitationSet: the trials for one datum.
# WHY:  The bonus asks for an NCT ID and an exact value for each datum.


class EvidenceItem(BaseModel):
    field: str = Field(description="Path of the value in the ClinicalTrials.gov study record.")
    value: Any = Field(description="The value exactly as the API returned it.")


class CitationItem(BaseModel):
    nct_id: str
    title: str | None = Field(default=None, description="The trial's brief title, so a person can tell what it is.")
    url: str
    bucket_label: str = Field(description="The bar, point, node or edge this trial supports.")
    evidence: list[EvidenceItem]


class CitationSet(BaseModel):
    total: int = Field(description="Exact number of trials behind the datum.")
    shown: int = Field(description="Number of items listed, at most `max_citations`.")
    is_sample: bool = Field(
        default=False, description="True when items were drawn from a sample of the trials."
    )
    search_url: str = Field(description="clinicaltrials.gov search for the trials behind this datum.")
    search_is_exact: bool = Field(
        default=True,
        description="True when `search_url` returns exactly the `total` trials. False when it is "
        "the closest search available: ClinicalTrials.gov matches free-text names loosely "
        "(ignoring case and hyphens, and including some synonyms), so it can list a few more.",
    )
    items: list[CitationItem] = Field(default_factory=list)


# ===== PART 8: Visualization ======================================================================
# WHAT: One model for each chart type, with its encoding.
# WHY:  A frontend can draw each chart from a fixed structure.


class EncodingChannel(BaseModel):
    """Connects one visual channel to a key of the data rows."""

    field: str = Field(description="Key in each data row.")
    type: ChannelType
    title: str
    unit: str | None = None


class BarEncoding(BaseModel):
    x: EncodingChannel
    y: EncodingChannel


class GroupedBarEncoding(BaseModel):
    x: EncodingChannel
    y: EncodingChannel
    series: EncodingChannel


class StackedBarEncoding(BaseModel):
    x: EncodingChannel
    y: EncodingChannel
    stack: EncodingChannel


class HeatmapEncoding(BaseModel):
    x: EncodingChannel
    y: EncodingChannel
    color: EncodingChannel


class TimeSeriesEncoding(BaseModel):
    x: EncodingChannel
    y: EncodingChannel
    series: EncodingChannel | None = None


class HistogramEncoding(BaseModel):
    x: EncodingChannel = Field(description="The binned field; rows carry `bin_start`, `bin_end`.")
    y: EncodingChannel


class ScatterEncoding(BaseModel):
    x: EncodingChannel
    y: EncodingChannel
    series: EncodingChannel | None = None


class GanttEncoding(BaseModel):
    y: EncodingChannel
    start: EncodingChannel
    end: EncodingChannel


class NetworkNodeEncoding(BaseModel):
    label: EncodingChannel
    size: EncodingChannel
    color: EncodingChannel


class NetworkEdgeEncoding(BaseModel):
    source: str = "source"
    target: str = "target"
    weight: EncodingChannel


class NetworkEncoding(BaseModel):
    nodes: NetworkNodeEncoding
    edges: NetworkEdgeEncoding


class HistogramRow(BaseModel):
    id: str
    bin_start: float = Field(description="Inclusive lower edge.")
    bin_end: float = Field(description="Exclusive upper edge; for the overflow bar, the largest value.")
    trial_count: int
    is_overflow: bool = Field(
        default=False, description="True for a last bar that gathers every value from `bin_start` up."
    )


class GanttRow(BaseModel):
    id: str
    nct_id: str
    label: str = Field(description="The trial's brief title, in full (often over 100 characters); the NCT id if it has none.")
    start: str
    end: str


class _Chart(BaseModel):
    title: str


class BarChart(_Chart):
    type: Literal[ChartType.BAR_CHART] = ChartType.BAR_CHART
    encoding: BarEncoding
    data: list[DataRow] = Field(description="Rows of {id, <x>, trial_count}.")


class GroupedBarChart(_Chart):
    type: Literal[ChartType.GROUPED_BAR_CHART] = ChartType.GROUPED_BAR_CHART
    encoding: GroupedBarEncoding
    data: list[DataRow] = Field(description="Rows of {id, <x>, series, trial_count}.")


class StackedBarChart(_Chart):
    type: Literal[ChartType.STACKED_BAR_CHART] = ChartType.STACKED_BAR_CHART
    encoding: StackedBarEncoding
    data: list[DataRow] = Field(description="Rows of {id, <x>, <stack>, trial_count}.")


class Heatmap(_Chart):
    type: Literal[ChartType.HEATMAP] = ChartType.HEATMAP
    encoding: HeatmapEncoding
    data: list[DataRow] = Field(description="Rows of {id, <x>, <y>, trial_count}.")


class TimeSeries(_Chart):
    type: Literal[ChartType.TIME_SERIES] = ChartType.TIME_SERIES
    encoding: TimeSeriesEncoding
    data: list[DataRow] = Field(description="Rows of {id, year, series?, trial_count}.")


class Histogram(_Chart):
    type: Literal[ChartType.HISTOGRAM] = ChartType.HISTOGRAM
    encoding: HistogramEncoding
    data: list[HistogramRow]


class ScatterPlot(_Chart):
    type: Literal[ChartType.SCATTER_PLOT] = ChartType.SCATTER_PLOT
    encoding: ScatterEncoding
    data: list[DataRow] = Field(description="Rows of {id, nct_id, <x>, <y>}.")


class Gantt(_Chart):
    type: Literal[ChartType.GANTT] = ChartType.GANTT
    encoding: GanttEncoding
    data: list[GanttRow]


class NetworkGraph(_Chart):
    type: Literal[ChartType.NETWORK_GRAPH] = ChartType.NETWORK_GRAPH
    encoding: NetworkEncoding
    data: NetworkData


Visualization = Annotated[
    BarChart
    | GroupedBarChart
    | StackedBarChart
    | Heatmap
    | TimeSeries
    | Histogram
    | ScatterPlot
    | Gantt
    | NetworkGraph,
    Field(discriminator="type"),
]

# ===== PART 9: Run state and response =============================================================
# WHAT: RunState, Suggestion, and VisualizationResponse.
# WHY:  All results have the same response structure.


class TraceStep(BaseModel):
    """One pipeline step. The details of the step are extra keys next to `step` and `ms`."""

    model_config = ConfigDict(extra="allow")

    step: str = Field(description="plan | validate | fetch | aggregate | build")
    ms: float = Field(description="Wall-clock duration in milliseconds.")


class RunState(BaseModel):
    """All facts about one request while it moves through the pipeline."""

    request: QueryRequest
    plan: QueryPlan | None = None
    notes: list[str] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    llm_exchanges: list[dict[str, Any]] = Field(
        default_factory=list,
        description="What was sent to each language model and what it returned, word for word. "
        "For the run log only; never part of a response.",
    )

    def record(self, step: str, ms: float, **details: Any) -> None:
        self.trace.append(TraceStep(step=step, ms=round(ms, 1), **details))

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


class Suggestion(BaseModel):
    """A question that the user can select when the service cannot answer the original question.

    It is a different question (`query`), or the same question with more request fields
    (`request`). Example: a status filter that makes a too-large question small enough.
    """

    label: str = Field(description="Text for a button: the reworded question, or what is narrowed.")
    query: str | None = Field(default=None, description="A question to ask instead. None: keep the question.")
    request: dict[str, Any] = Field(default_factory=dict, description="Request fields to add.")
    trials_matched: int | None = Field(
        default=None, description="How many trials this version matches, when that was counted."
    )


class ResponseMeta(BaseModel):
    plan: QueryPlan | None = Field(
        default=None, description="The validated plan: mode, grouping and series choices."
    )
    interpretation: str = Field(default="", description="The question as it was understood.")
    filters_applied: dict[str, Any] = Field(
        default_factory=dict, description="Series label -> filters sent to ClinicalTrials.gov."
    )
    search_terms: dict[str, list[str]] = Field(default_factory=dict)
    sort: SortOrder | None = None
    time_granularity: Literal["year"] | None = None
    units: dict[str, str] = Field(
        default_factory=dict, description='Encoding channel -> unit, e.g. {"y": "trials"}.'
    )
    trials_matched: int = Field(default=0, description="Trials matching the filters.")
    trials_used: int = Field(default=0, description="Trials that contributed to the chart.")
    tier: Literal[1, 2] | None = None
    chart_overridden: bool = Field(
        default=False, description="True when the suggested chart type was replaced."
    )
    pruning: PruningInfo | None = None
    notes: list[str] = Field(
        default_factory=list, description="Assumptions, drops and overrides, in plain language."
    )
    source: str = "clinicaltrials.gov"
    data_timestamp: str | None = None
    retrieved_at: str | None = Field(default=None, description="ISO 8601 UTC time of this run.")
    suggestions: list[Suggestion] = Field(
        default_factory=list,
        description="For an `ok` answer that matched no trials: close questions that do match "
        "some, usually the same question with one filter dropped, each with its trial count.",
    )
    trace: list[TraceStep] | None = Field(default=None, description="Present when `debug` is true.")


class ErrorInfo(BaseModel):
    code: str = Field(description="Stable machine-readable reason.")
    message: str = Field(description="Plain-language explanation, safe to show to a user.")
    details: list[str] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(
        default_factory=list,
        description="For `unsupported` responses: close alternatives that can be answered. "
        "Reworded questions for a vague or out-of-scope question; narrower versions with their "
        "trial counts for a question that matched too many trials. None is applied automatically.",
    )


class VisualizationResponse(BaseModel):
    """The body of each `POST /query` response, for all results."""

    status: ResponseStatus
    query: str
    visualization: Visualization | None = Field(
        default=None, description="Present when `status` is ok."
    )
    citations: dict[str, CitationSet] = Field(
        default_factory=dict, description="Datum `id` -> the trials behind it."
    )
    meta: ResponseMeta = Field(default_factory=ResponseMeta)
    error: ErrorInfo | None = Field(default=None, description="Present when `status` is not ok.")


class SchemaResponse(BaseModel):
    """The body of `GET /schema`."""

    request: dict[str, Any]
    response: dict[str, Any]
