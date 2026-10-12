"""TASK:     Counts trials for each bucket. Gives bar, grouped, stacked, heatmap, and time charts.
GRADING:  Query and Visualization Coverage. One mode gives five chart types.
INPUT:    The fetched series and the plan (mode `aggregate`).
OUTPUT:   An AggregateResult: one row for each cell, with its trial count and trial IDs.
USED BY:  app/pipeline.py (the AGGREGATORS table)
DIAGRAM:  3 (from the plan to the chart type).

PARTS:
    1. The main function: aggregate
    2. The order and the limit of the buckets
    3. The notes for the user

The result always has the same grid structure, for all questions:
    series  x  bucket of the first group_by  x  bucket of the second group_by  ->  trial_count
Each cell keeps the NCT IDs of its trials. Thus each number can have citations.
"""
from collections.abc import Hashable
from dataclasses import dataclass, field
from itertools import product

from app.aggregate.common import Dimension, year_range
from app.models import (
    COUNT_KEY,
    SERIES_KEY,
    UNKNOWN_LABEL,
    AggregateResult,
    BucketRef,
    DatumAbout,
    DataRow,
    FieldKind,
    MultiRule,
    PlanMode,
    QueryPlan,
    SeriesFetch,
    SortOrder,
)
from app.registry import REGISTRY, row_key

# ===== PART 1: The main function: aggregate =======================================================
# WHAT: Puts each trial in its cells and counts the trials of each cell.
# WHY:  This one function answers all questions that count trials by one or two fields.


@dataclass
class _Cell:
    """WHAT: One cell of the grid: the trial IDs in it, and the exact count from the API if there is one."""

    nct_ids: list[str] = field(default_factory=list)
    exact: int | None = None  # Tier 2: the count from the API. `nct_ids` is then a sample only.

    @property
    def count(self) -> int:
        """WHAT: Returns the number of trials in the cell. In tier 2, this is the exact count from the API."""
        return len(self.nct_ids) if self.exact is None else self.exact


def aggregate(series: list[SeriesFetch], plan: QueryPlan) -> AggregateResult:
    """WHAT:   Counts the trials for each bucket.
    INPUT:  series - the fetched series. plan - the plan.
    OUTPUT: An AggregateResult with the rows, the trial IDs for each row, and the notes.
            A series that was not fetched is skipped. The caller reports it.
    """
    if not 1 <= len(plan.group_by) <= 2:
        raise ValueError("aggregate mode needs one or two group_by fields")
    series = [s for s in series if s.error is None]
    if len(plan.group_by) == 2 and len(series) > 1:
        raise ValueError("a comparison of several series can be broken down by one field only")

    dims = [Dimension(REGISTRY[name], plan.bin) for name in plan.group_by]
    x = dims[0]
    cells: dict[tuple[str, tuple[Hashable, ...]], _Cell] = {}
    dropped: dict[str, int] = {}
    trials_used = 0
    tier2 = any(s.tier == 2 for s in series)
    buckets_exceed_trials = False
    by_start_year = any(dim.field.name == "start_year" for dim in dims)
    planned_starts = 0

    for s in series:
        if s.tier == 2:
            # The exact counts come from the API. The fetched trials are samples for citations only.
            for label, n in (s.bucket_counts or {}).items():
                cells[(s.label, (label,))] = _Cell(exact=n)
            for trial in s.trials:
                for key in x.keys(trial) or []:
                    if (s.label, (key,)) in cells:
                        cells[(s.label, (key,))].nct_ids.append(trial.nct_id)
            trials_used += s.trials_matched
            buckets_exceed_trials |= sum((s.bucket_counts or {}).values()) > s.trials_matched
            continue
        for trial in s.trials:
            keys = [dim.keys(trial) for dim in dims]
            missing = [dim.field.name for dim, k in zip(dims, keys) if k is None]
            if missing:
                dropped[missing[0]] = dropped.get(missing[0], 0) + 1
                continue
            trials_used += 1
            planned_starts += by_start_year and trial.start_date_type == "ESTIMATED"
            for combination in product(*keys):
                cells.setdefault((s.label, combination), _Cell()).nct_ids.append(trial.nct_id)

    notes: list[str] = []
    x_keys = _ordered_keys(x, 0, cells, plan, notes, zero_fill=True)
    second_keys = _ordered_keys(dims[1], 1, cells, plan, notes) if len(dims) == 2 else [None]

    rows: list[DataRow] = []
    contributing: dict[str, list[str]] = {}
    about: dict[str, DatumAbout] = {}
    exact_totals: dict[str, int] = {}
    with_series = len(plan.series) > 1
    for x_key in x_keys:
        for s, second_key in product(series, second_keys):
            cell = cells.get((s.label, (x_key,) if second_key is None else (x_key, second_key)), _Cell())
            row: DataRow = {"id": f"d{len(rows) + 1}", row_key(x.field.name): x.label(x_key)}
            if with_series:
                row[SERIES_KEY] = s.label
            if second_key is not None:
                row[row_key(dims[1].field.name)] = dims[1].label(second_key)
            row[COUNT_KEY] = cell.count
            rows.append(row)
            contributing[row["id"]] = list(dict.fromkeys(cell.nct_ids))
            buckets = [BucketRef(field=dim.field.name, bucket=row[row_key(dim.field.name)]) for dim in dims]
            names = ([s.label] if with_series else []) + [str(bucket.bucket) for bucket in buckets]
            about[row["id"]] = DatumAbout(series=s.label, label=" · ".join(names), buckets=buckets)
            if cell.exact is not None:
                exact_totals[row["id"]] = cell.exact

    shown = [x_keys, second_keys]
    notes += _notes(dims, cells, dropped, trials_used, buckets_exceed_trials, planned_starts, shown)
    return AggregateResult(
        mode=PlanMode.AGGREGATE,
        rows=rows,
        contributing=contributing,
        about=about,
        exact_totals=exact_totals if tier2 else None,
        trials_used=trials_used,
        dropped=dropped,
        notes=notes,
    )


# ===== PART 2: The order and the limit of the buckets =============================================
# WHAT: Sorts the buckets and keeps the number that the plan asks for.
# WHY:  A time axis must stay complete and in time order.


def _ordered_keys(
    dim: Dimension,
    position: int,
    cells: dict[tuple[str, tuple[Hashable, ...]], _Cell],
    plan: QueryPlan,
    notes: list[str],
    zero_fill: bool = False,
) -> list[Hashable]:
    """WHAT:   Returns the buckets of one dimension, sorted, and cut to the limit of the plan.
    WHY:    A time axis sorted by size, or cut to the ten largest years, would show a wrong trend.
    OUTPUT: The bucket keys in display order. If buckets are cut, a note is added.
    """
    totals: dict[Hashable, int] = {}
    for (_, keys), cell in cells.items():
        totals[keys[position]] = totals.get(keys[position], 0) + cell.count
    if dim.field.kind is FieldKind.TIME:
        return year_range(totals) if zero_fill else sorted(totals)

    keys = sorted(totals, key=dim.natural_position)
    if plan.sort is not SortOrder.NATURAL:
        keys.sort(key=lambda k: totals[k], reverse=plan.sort is SortOrder.DESC)  # A stable sort: equal values keep their natural order.
    if len(keys) > plan.limit:
        which = "fewest" if plan.sort is SortOrder.ASC else "most"
        if plan.sort is SortOrder.NATURAL:
            kept = set(sorted(totals, key=lambda k: totals[k], reverse=True)[: plan.limit])
            keys = [k for k in keys if k in kept]
        else:
            keys = keys[: plan.limit]
        notes.append(
            f"{len(totals):,} different values of {dim.field.display_name.lower()} were found; "
            f"only the {plan.limit} with the {which} trials are shown."
        )
    return keys


# ===== PART 3: The notes for the user =============================================================
# WHAT: Writes a plain-language note for each thing that changes how to read the chart.
# WHY:  The user must know about each removal, merge, and limit (P4).


def _trials_have(n: int) -> str:
    """WHAT: Returns "1 trial has" or "n trials have", for a note."""
    return "1 trial has" if n == 1 else f"{n:,} trials have"


def _are(n: int) -> str:
    """WHAT: Returns "is" or "are", for a note."""
    return "is" if n == 1 else "are"


def _trials_in_bucket(
    cells: dict[tuple[str, tuple[Hashable, ...]], _Cell], position: int, key: Hashable
) -> int:
    """WHAT: Returns the number of trials in one bucket of one dimension, for all series together.
    WHY:  With two dimensions, one trial can be in two or more cells of the same bucket.
          Thus the function counts trials and not cells.
    """
    exact = 0
    per_series: dict[str, set[str]] = {}
    for (series_label, keys), cell in cells.items():
        if keys[position] != key:
            continue
        if cell.exact is not None:
            exact += cell.exact
        else:
            per_series.setdefault(series_label, set()).update(cell.nct_ids)
    return exact + sum(len(ids) for ids in per_series.values())


def _notes(
    dims: list[Dimension],
    cells: dict[tuple[str, tuple[Hashable, ...]], _Cell],
    dropped: dict[str, int],
    trials_used: int,
    buckets_exceed_trials: bool,
    planned_starts: int,
    shown: list[list[Hashable]],
) -> list[str]:
    """WHAT: Returns the plain-language notes for the chart: unknown values, merged spellings, removed trials, and limits."""
    notes = []
    for position, dim in enumerate(dims):
        name = dim.field.display_name.lower()
        if dim.field.multi is MultiRule.EXPLODE and (dim.multi_valued or buckets_exceed_trials):
            notes.append(
                f"Trials with more than one {name} are counted once per {name}, "
                f"so the bars add up to more than the {trials_used:,} trials found."
            )
        if dim.merged_spellings:
            details = "capital letters, hyphens"
            if dim.field.merge_spellings == "company":
                details += ", punctuation, a trailing Inc. or Ltd"
            notes.append(
                f"{dim.field.display_name} names written in slightly different ways ({details}) are "
                f"counted together and shown under the most common spelling."
            )
        unknown = _trials_in_bucket(cells, position, UNKNOWN_LABEL)
        if unknown and UNKNOWN_LABEL in shown[position]:
            notes.append(f'{_trials_have(unknown)} no {name} listed and {_are(unknown)} shown as "{UNKNOWN_LABEL}".')
        elif unknown:  # The limit removed this bucket. Do not write a note about a bar that is not in the chart.
            notes.append(f"{_trials_have(unknown)} no {name} listed; that group is not among those shown.")
        left_out = dropped.get(dim.field.name, 0)
        if left_out:
            notes.append(f"{_trials_have(left_out)} no {name} and {_are(left_out)} left out.")
    if planned_starts:
        notes.append(
            f"{_trials_have(planned_starts)} a planned start date that ClinicalTrials.gov marks as "
            f"estimated, not actual; {'it is' if planned_starts == 1 else 'they are'} counted in the "
            f"planned year."
        )
    return notes
