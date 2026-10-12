"""TASK:     Makes one mark for each trial: a scatter dot or a timeline bar.
GRADING:  Query and Visualization Coverage.
INPUT:    The fetched series and the plan (mode `points`).
OUTPUT:   An AggregateResult: one row for each trial.
USED BY:  app/pipeline.py (the AGGREGATORS table)

PARTS:
    1. Small functions
    2. The main function: points
    3. Scatter plot
    4. Timeline

    Scatter    x_field and y_field: two numbers for each trial.
    Timeline   start_field and end_field: two dates for each trial (a gantt bar).

A trial that does not have the two values cannot be on the chart. The code removes it and
counts it. When there are too many trials to draw, the code shows a fixed selection.
"""
from app.models import (
    SERIES_KEY,
    AggregateResult,
    BucketRef,
    DataRow,
    DatumAbout,
    PlanMode,
    QueryPlan,
    SeriesFetch,
    SortOrder,
    Trial,
)
from app.registry import REGISTRY

# ===== PART 1: Small functions ====================================================================
# WHAT: The limit of marks, and functions for notes, dates, and selection.
# WHY:  A chart with thousands of marks cannot be read.

MAX_POINTS = 500  # The maximum number of dots in a scatter plot. Each dot has its own citation.
ESTIMATED = "ESTIMATED"


def _trials_have(n: int) -> str:
    """WHAT: Returns "1 trial has" or "n trials have", for a note."""
    return "1 trial has" if n == 1 else f"{n:,} trials have"


def _are(n: int) -> str:
    """WHAT: Returns "is" or "are", for a note."""
    return "is" if n == 1 else "are"


def _about(series: SeriesFetch, trial: Trial, fields: list[str]) -> DatumAbout:
    """WHAT: Returns what one mark contains: its series, its trial, and its fields. Citations use this."""
    return DatumAbout(
        series=series.label,
        label=trial.nct_id,
        buckets=[BucketRef(field=name) for name in fields],
        search=[f"AREA[NCTId]{trial.nct_id}"],
        search_is_exact=True,
    )


def _in_order(start: str, end: str) -> bool:
    """WHAT: Returns True if the start is not after the end.
    The function compares only the parts that the two dates have. `2020-05` and `2020-05-17`
    are in the same month, so they are in order.
    """
    shared = min(len(start), len(end))
    return start[:shared] <= end[:shared]


def _evenly_spaced(items: list, limit: int) -> list:
    """WHAT: Returns `limit` items at equal intervals in the list. The items are the same on each run."""
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


# ===== PART 2: The main function: points ==========================================================
# WHAT: Selects the scatter plot or the timeline from the fields of the plan.
# WHY:  One plan mode gives two chart types.


def points(series: list[SeriesFetch], plan: QueryPlan) -> AggregateResult:
    """WHAT:   Makes one datum for each trial.
    INPUT:  series - the fetched series. plan - the plan.
    OUTPUT: An AggregateResult. The field pair of the plan selects a scatter plot or a timeline.
    """
    series = [s for s in series if s.error is None]
    result = AggregateResult(mode=PlanMode.POINTS)
    if plan.x_field and plan.y_field:
        _scatter(series, plan, result)
    else:
        _timeline(series, plan, result)
    return result


# ===== PART 3: Scatter plot =======================================================================
# WHAT: Makes one dot for each trial from two number fields.
# WHY:  The user can see the relation between two numbers.


def _scatter(series: list[SeriesFetch], plan: QueryPlan, result: AggregateResult) -> None:
    """WHAT: Adds one row for each trial that has the two numbers. Removes and counts the other trials."""
    fields = [plan.x_field, plan.y_field]
    names = " or ".join(REGISTRY[name].display_name.lower() for name in fields)
    with_series = len(plan.series) > 1
    usable: list[tuple[SeriesFetch, Trial]] = []
    missing = 0
    for s in series:
        for trial in sorted(s.trials, key=lambda t: t.nct_id):
            if any(getattr(trial, name) is None for name in fields):
                missing += 1
            else:
                usable.append((s, trial))

    shown = _evenly_spaced(usable, MAX_POINTS)
    for s, trial in shown:
        row: DataRow = {"id": f"d{len(result.rows) + 1}", "nct_id": trial.nct_id}
        if with_series:
            row[SERIES_KEY] = s.label
        row.update({name: getattr(trial, name) for name in fields})
        result.rows.append(row)
        result.contributing[row["id"]] = [trial.nct_id]
        result.about[row["id"]] = _about(s, trial, fields)

    result.trials_used = len(shown)
    if missing:
        result.dropped[" or ".join(fields)] = missing
        result.notes.append(f"{_trials_have(missing)} no {names} and {_are(missing)} left out.")
    if len(usable) > len(shown):
        result.dropped["over the point limit"] = len(usable) - len(shown)
        result.notes.append(
            f"{len(usable):,} trials could be plotted; {len(shown):,} of them, spread evenly by registration "
            f"number, are shown."
        )
    _note_planned_end(result, [trial for _, trial in shown], fields, "duration is an estimate")


# ===== PART 4: Timeline ===========================================================================
# WHAT: Makes one bar for each trial from its start date and end date.
# WHY:  The user can see when each trial ran.


def _timeline(series: list[SeriesFetch], plan: QueryPlan, result: AggregateResult) -> None:
    """WHAT: Adds one row for each trial that has the two dates in the correct order. Removes and counts the other trials."""
    fields = [plan.start_field, plan.end_field]
    source = series[0] if series else None
    trials = source.trials if source else []
    dated = [t for t in trials if all(getattr(t, name) for name in fields)]
    dated = [t for t in dated if _in_order(getattr(t, fields[0]), getattr(t, fields[1]))]
    missing = len(trials) - len(dated)

    # ISO dates sort correctly as text. The NCT ID decides between equal dates, so each run gives the same selection.
    latest_first = plan.sort is SortOrder.DESC
    dated.sort(key=lambda t: t.nct_id)
    dated.sort(key=lambda t: getattr(t, fields[0]), reverse=latest_first)
    shown = dated[: plan.limit]
    for trial in shown:
        row_id = f"d{len(result.rows) + 1}"
        result.rows.append(
            {
                "id": row_id,
                "nct_id": trial.nct_id,
                "label": trial.brief_title or trial.nct_id,
                "start": getattr(trial, fields[0]),
                "end": getattr(trial, fields[1]),
            }
        )
        result.contributing[row_id] = [trial.nct_id]
        result.about[row_id] = _about(source, trial, fields)

    result.trials_used = len(shown)
    if missing:
        result.dropped["no usable dates"] = missing
        result.notes.append(
            f"{_trials_have(missing)} no start date or no completion date (or dates in the wrong order) "
            f"and {_are(missing)} left out."
        )
    if len(dated) > len(shown):
        result.dropped["over the limit"] = len(dated) - len(shown)
        which = "most recently started" if latest_first else "earliest to start"
        result.notes.append(f"{len(dated):,} trials have both dates; the {len(shown)} {which} are shown.")
    _note_planned_end(result, shown, fields, "bar ends at a planned date")


def _note_planned_end(result: AggregateResult, shown: list[Trial], fields: list[str], consequence: str) -> None:
    """WHAT: Adds a note where a planned completion date is used.
    WHY:  Most trials in progress have only a planned completion date.
    """
    if not {"completion_date", "duration_months"} & set(fields):
        return
    planned = sum(trial.completion_date_type == ESTIMATED for trial in shown)
    if planned:
        result.notes.append(
            f"{planned:,} of the {len(shown):,} trials shown have a planned completion date, not an actual one, "
            f"so their {consequence}."
        )
