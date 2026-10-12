"""TASK:     Makes the citations for each bar, point, node, and edge.
GRADING:  Bonus (deep citations). Each datum gets NCT IDs and exact registry values.
INPUT:    The aggregated result, the fetched series, the plan, and `max_citations`.
OUTPUT:   Datum ID -> CitationSet.
USED BY:  app/pipeline.py
DIAGRAM:  7 (from a bar to the trial record).

PARTS:
    1. Fixed values
    2. Which trials to show first
    3. The evidence from the trial record
    4. The search link
    5. The main function: build_citations

All work here is deterministic. It uses data that the service fetched. The LLM does not write
a citation. Each CitationSet has:
    total        The exact number of trials counted in the datum.
    items        A maximum of `max_citations` trials, each with its raw registry value and path.
    search_url   A ClinicalTrials.gov search for the same filter and bucket.
"""
from collections.abc import Callable
from urllib.parse import urlencode

from app.models import (
    UNKNOWN_LABEL,
    AggregateResult,
    CitationItem,
    CitationSet,
    EvidenceItem,
    FieldKind,
    MultiRule,
    OverallStatus,
    QueryPlan,
    Scalar,
    SeriesFetch,
    Trial,
)
from app.registry import REGISTRY, bucket_expression, bucket_search_is_exact, match_key

# ===== PART 1: Fixed values =======================================================================
# WHAT: The fields that citations need, the limits, and the URL forms.
# WHY:  The pipeline reads these fields only when the request wants citations.

# The fields that citations read: the title of each trial, and the fields that the rank uses.
# The pipeline reads them only when the request wants citations.
CITATION_FIELDS = ("brief_title", "sponsor", "last_updated", "enrollment", "has_results")
MAX_PER_SPONSOR = 2
MAX_SPELLINGS_IN_URL = 10
STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"
SEARCH_URL = "https://clinicaltrials.gov/search"
# API query parameter -> the same search box on the clinicaltrials.gov website.
_SITE_PARAMS = {"query.cond": "cond", "query.intr": "intr", "query.spons": "spons", "query.term": "term"}

RankKey = Callable[[Trial], tuple]


# ===== PART 2: Which trials to show first =========================================================
# WHAT: Sorts the trials of a datum, so that the best examples are first.
# WHY:  The best example depends on the question. For recruiting trials, it is the last updated trial.


def _recent(trial: Trial) -> str:
    """WHAT: Sort key: the last update date of a trial."""
    return trial.last_updated or ""


def _large(trial: Trial) -> int:
    """WHAT: Sort key: the enrollment of a trial."""
    return trial.enrollment if trial.enrollment is not None else -1


def _has_results(trial: Trial) -> bool:
    """WHAT: Sort key: 1 if the trial has posted results, 0 if not."""
    return bool(trial.has_results)


# The best trials to show depend on the question. The larger tuple is first.
_RANKINGS: dict[str, RankKey] = {
    "recruiting": lambda t: (_recent(t), _large(t)),  # Trials that recruit now: the last updated record is first.
    "completed": lambda t: (_has_results(t), _large(t)),  # Completed trials: trials with results are first.
    "time_bucket": lambda t: (_large(t), _has_results(t)),  # A year bucket: the largest trials are first.
    "default": lambda t: (_has_results(t), _large(t), _recent(t)),
}


def ranking_rule(series: SeriesFetch, plan: QueryPlan) -> str:
    """WHAT:   Returns the name of the rank rule for the data points of a series.
    INPUT:  series - the fetched series. plan - the plan.
    OUTPUT: "recruiting", "completed", "year", or "default".
    """
    status = series.filters.status or []
    if status == [OverallStatus.RECRUITING]:
        return "recruiting"
    if status == [OverallStatus.COMPLETED]:
        return "completed"
    if any(REGISTRY[name].kind is FieldKind.TIME for name in plan.group_by):
        return "time_bucket"
    return "default"


def rank(trials: list[Trial], rule: str, limit: int) -> list[Trial]:
    """WHAT:   Returns the trials to show, best first.
    WHY:    A list with one sponsor only is a weak example. Thus a maximum of two trials for
            each sponsor are listed while other sponsors remain.
    INPUT:  trials - the trials of the datum. rule - the rank rule. limit - the number to return.
    OUTPUT: A maximum of `limit` trials.
    """
    ordered = sorted(trials, key=lambda t: t.nct_id)  # Equal trials get a fixed order.
    ordered.sort(key=_RANKINGS[rule], reverse=True)
    chosen: list[Trial] = []
    held_back: list[Trial] = []
    per_sponsor: dict[str, int] = {}
    for trial in ordered:
        sponsor = (trial.sponsor or trial.nct_id).casefold()
        if per_sponsor.get(sponsor, 0) < MAX_PER_SPONSOR:
            per_sponsor[sponsor] = per_sponsor.get(sponsor, 0) + 1
            chosen.append(trial)
        else:
            held_back.append(trial)
    return (chosen + held_back)[:limit]


# ===== PART 3: The evidence from the trial record =================================================
# WHAT: Finds the raw value in the record that put the trial in the datum.
# WHY:  The assignment asks for an exact value from the API response.


def evidence_for(trial: Trial, field_name: str, bucket: Scalar) -> list[EvidenceItem]:
    """WHAT:   Returns the raw values in the trial record that put the trial in a bucket.
    INPUT:  trial - the trial. field_name - the field. bucket - the bucket label.
    OUTPUT: A list of evidence items. For a list field, only the entry that matches is quoted.
            A trial in the "Unknown" bucket gets evidence with a null value.
    """
    entries = trial.evidence.get(field_name, [])
    if REGISTRY[field_name].multi is MultiRule.EXPLODE:
        wanted = match_key(field_name, bucket)  # The bar can show a different spelling of this name.
        entries = [entry for entry in entries if match_key(field_name, entry.label) == wanted]
    if not entries:
        return [EvidenceItem(field=REGISTRY[field_name].path or field_name, value=None)]
    return [EvidenceItem(field=entry.path, value=entry.raw) for entry in entries]


def _spellings(trials: list[Trial], field_name: str, bucket: Scalar) -> list[str]:
    """WHAT: Returns the different original texts of a bucket, so that the search link matches each of them."""
    found: dict[str, None] = {}
    for trial in trials:
        for item in evidence_for(trial, field_name, bucket):
            if isinstance(item.value, str):
                found[item.value] = None
    return list(found) if len(found) <= MAX_SPELLINGS_IN_URL else []


# ===== PART 4: The search link ====================================================================
# WHAT: Makes a ClinicalTrials.gov link that lists the trials of the datum.
# WHY:  The user can see all trials, not only the listed ones.


def search_url(api_params: dict[str, str], expressions: list[str | None]) -> str:
    """WHAT:   Returns a clinicaltrials.gov search that shows the trials of one datum.
    INPUT:  api_params - the API parameters of the series. expressions - the bucket expressions.
    OUTPUT: The URL. Filters with no box on the website go into the "Other terms" box.
    """
    site: dict[str, str] = {}
    terms: list[str] = []
    status = REGISTRY["status"]
    for name, value in api_params.items():
        if name in _SITE_PARAMS:
            site[_SITE_PARAMS[name]] = value
        elif name == status.filter_param:
            codes = " OR ".join(f"AREA[{status.api_field}]{code}" for code in value.split(","))
            terms.append(f"({codes})")
        elif name == "filter.advanced":
            terms.append(value)
    terms += [expression for expression in expressions if expression]
    if terms:
        own = [f"({site['term']})"] if "term" in site else []
        site["term"] = " AND ".join(own + terms)
    return f"{SEARCH_URL}?{urlencode(site)}" if site else SEARCH_URL


# ===== PART 5: The main function: build_citations =================================================
# WHAT: Makes one CitationSet for each datum.
# WHY:  One function makes the citations for all chart types.


def build_citations(
    result: AggregateResult, series: list[SeriesFetch], plan: QueryPlan, max_citations: int
) -> dict[str, CitationSet]:
    """WHAT:   Makes one CitationSet for each datum.
    WHY:    Each aggregation mode tells what its data points contain (`result.about`).
            Thus this one function works for all chart types.
    INPUT:  result - the aggregated result. series - the fetched series. plan - the plan.
            max_citations - the maximum number of trials to list for each datum.
    OUTPUT: Datum ID -> CitationSet. A datum with no trials gets a set with total 0.
    """
    fetched = {s.label: s for s in series if s.error is None}
    trials_of = {label: {trial.nct_id: trial for trial in s.trials} for label, s in fetched.items()}
    citations: dict[str, CitationSet] = {}

    for datum_id, about in result.about.items():
        source = fetched.get(about.series)
        if source is None:
            continue
        lookup = trials_of[source.label]
        trials = [lookup[nct_id] for nct_id in result.contributing.get(datum_id, []) if nct_id in lookup]

        shown = rank(trials, ranking_rule(source, plan), max_citations)
        items = [
            CitationItem(
                nct_id=trial.nct_id,
                title=trial.brief_title,
                url=STUDY_URL.format(nct_id=trial.nct_id),
                bucket_label=about.label,
                evidence=[item for ref in about.buckets for item in evidence_for(trial, ref.field, ref.bucket)],
            )
            for trial in shown
        ]
        if about.search is None:
            expressions = [
                bucket_expression(ref.field, ref.bucket, _spellings(trials, ref.field, ref.bucket))
                for ref in about.buckets
            ]
            exact = all(ref.bucket == UNKNOWN_LABEL or bucket_search_is_exact(ref.field) for ref in about.buckets)
        else:
            expressions, exact = list(about.search), bool(about.search_is_exact)
        citations[datum_id] = CitationSet(
            total=(result.exact_totals or {}).get(datum_id, len(trials)),
            shown=len(items),
            is_sample=source.is_sample,
            search_url=search_url(source.api_params, expressions),
            search_is_exact=exact,
            items=items,
        )
    return citations
