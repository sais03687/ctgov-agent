"""TASK:     Counts trials in number ranges for a histogram.
GRADING:  Coverage. System Design (handles very large values in real data).
INPUT:    The fetched series and the plan (mode `histogram`).
OUTPUT:   An AggregateResult: one row for each bin, with its trial count and trial IDs.
USED BY:  app/pipeline.py (the AGGREGATORS table)

PARTS:
    1. The bin width
    2. The main function: histogram

The plan suggests a bin width. Code makes the final decision, because the planner did not see
the data. Trial sizes have a very long tail: some studies have a million participants.
    - If the suggested width needs more than MAX_BINS bars for the lowest 95 % of the trials,
      the code increases the width to the next round number that fits.
    - Values after the last regular bar go into one overflow bar.
"""
import math

from app.aggregate.common import bin_start
from app.models import AggregateResult, BucketRef, DatumAbout, PlanMode, QueryPlan, SeriesFetch
from app.registry import REGISTRY, range_expression

# ===== PART 1: The bin width ======================================================================
# WHAT: Finds a round bin width that fits the data in the available bars.
# WHY:  A width from the LLM can give thousands of bars.

MAX_BINS = 40
BULK_SHARE = 0.95


def nice_width(needed: float) -> float:
    """WHAT: Returns the smallest of 1, 2, 5, 10, 20, 50, ... that is not smaller than `needed`."""
    if needed <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(needed))
    for step in (1, 2, 5, 10):
        if step * magnitude >= needed:
            return float(step * magnitude)
    return float(10 * magnitude)


def _number(value: float) -> str:
    """WHAT: Returns a value as a number, or None if it is not a number."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,g}"


# ===== PART 2: The main function: histogram =======================================================
# WHAT: Counts the trials in each bin.
# WHY:  The chart must show the main part of the data clearly.


def histogram(series: list[SeriesFetch], plan: QueryPlan) -> AggregateResult:
    """WHAT:   Counts the trials in each bin of the number field of the plan.
    INPUT:  series - the fetched series. plan - the plan.
    OUTPUT: An AggregateResult. Trials with no value are removed and counted in a note.
    """
    field = REGISTRY[plan.bin.field]
    unit = f" {field.unit}" if field.unit else ""
    source = next((s for s in series if s.error is None), None)
    trials = source.trials if source else []

    valued = [(getattr(trial, field.name), trial.nct_id) for trial in trials]
    missing = sum(value is None for value, _ in valued)
    valued = sorted((value, nct_id) for value, nct_id in valued if value is not None)
    notes: list[str] = []
    result = AggregateResult(mode=PlanMode.HISTOGRAM, trials_used=len(valued))
    if missing:
        result.dropped[field.name] = missing
        verb = "trial has" if missing == 1 else "trials have"
        notes.append(f"{missing:,} {verb} no {field.display_name.lower()} and {'is' if missing == 1 else 'are'} left out.")
    if not valued:
        result.notes = notes
        return result

    # 1. Find a width that puts most of the trials in the available bars.
    width = plan.bin.bin_width
    first = bin_start(valued[0][0], width)
    bulk_top = valued[max(0, math.ceil(len(valued) * BULK_SHARE) - 1)][0]
    if (bulk_top - first) / width >= MAX_BINS - 1:
        widened = nice_width((bulk_top - first) / (MAX_BINS - 1))
        notes.append(
            f"The bar width was changed from {_number(width)} to {_number(widened)}{unit}, "
            f"so that the chart has at most {MAX_BINS} bars."
        )
        width, first = widened, bin_start(valued[0][0], widened)

    # 2. Make the regular bars, then one overflow bar for all larger values.
    last_regular = min(bin_start(valued[-1][0], width), first + (MAX_BINS - 2) * width)
    overflow_from = last_regular + width
    regular_bars = round((last_regular - first) / width) + 1
    edges = [first + i * width for i in range(regular_bars)]
    members: list[list[str]] = [[] for _ in edges]  # By bar index, so that empty bars stay in the data as zeros.
    overflow: list[str] = []
    for value, nct_id in valued:
        if value >= overflow_from:
            overflow.append(nct_id)
        else:
            members[min(int((value - first) // width), regular_bars - 1)].append(nct_id)

    def add(low: float, high: float, nct_ids: list[str], is_overflow: bool) -> None:
        row_id = f"d{len(result.rows) + 1}"
        result.rows.append(
            {"id": row_id, "bin_start": low, "bin_end": high, "trial_count": len(nct_ids), "is_overflow": is_overflow}
        )
        result.contributing[row_id] = nct_ids
        search = range_expression(field.name, low, None if is_overflow else high - 1)
        label = f"{_number(low)}{unit} or more" if is_overflow else f"{_number(low)}–{_number(high)}{unit}"
        result.about[row_id] = DatumAbout(
            series=source.label,
            label=label,
            buckets=[BucketRef(field=field.name)],
            search=[search] if search else [],
            search_is_exact=search is not None,
        )

    for edge, nct_ids in zip(edges, members):
        add(edge, edge + width, nct_ids, False)
    if overflow:
        add(overflow_from, valued[-1][0], overflow, True)
        verb = "trial has" if len(overflow) == 1 else "trials have"
        notes.append(
            f"{len(overflow):,} {verb} {_number(overflow_from)}{unit} or more and "
            f"{'is' if len(overflow) == 1 else 'are'} gathered in the last bar."
        )
    result.notes = notes
    return result
