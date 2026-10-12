"""TASK:     Examines each LLM plan before the service uses it.
GRADING:  AI / Agent Design. A plan that is not valid does not run.
INPUT:    A QueryPlan from the planner.
OUTPUT:   A list of problems. An empty list means that the plan can run.
USED BY:  app/pipeline.py
DIAGRAM:  5 (the gates that the LLM output must pass). This file is gate 2.

PARTS:
    1. Limits and fixed lists
    2. The list of problems
    3. The main function: validate
    4. Rules for each plan mode
    5. Rules for all plan modes

The service uses the list of problems two times:
    1. It sends the list to the LLM for the one retry.
    2. It shows the list to the user if the retry also fails.
Thus each message tells what is wrong and what is correct.
"""
from datetime import date

from app.models import MIN_YEAR, FieldKind, PlanFilters, PlanMode, QueryPlan, ValidationIssue
from app.registry import REGISTRY, field_names

# ===== PART 1: Limits and fixed lists ==============================================================
# WHAT: The largest values that a plan can have, and the plan keys of each mode.
# WHY:  The LLM writes the plan. These limits keep the plan small and safe to run.

MAX_SERIES = 5
MAX_GROUP_BY = 2
MAX_LIMIT = 50
MAX_EDGES = 200
YEARS_AHEAD = 10

_GROUPABLE = (FieldKind.CATEGORICAL, FieldKind.TIME, FieldKind.BOOLEAN, FieldKind.NUMERIC)
_DATES = (FieldKind.DATE,)

# The plan keys that each mode uses. A key that is set but is not in the list of its mode is an error.
_MODE_KEYS: dict[PlanMode, set[str]] = {
    PlanMode.AGGREGATE: {"group_by", "bin"},
    PlanMode.HISTOGRAM: {"bin"},
    PlanMode.POINTS: {"x_field", "y_field", "start_field", "end_field"},
    PlanMode.NETWORK: {"network"},
}
_ALL_MODE_KEYS = set().union(*_MODE_KEYS.values())


def _names(*kinds: FieldKind) -> str:
    """WHAT: Returns the field names of the given kinds as one text, for a message."""
    return ", ".join(field_names(*kinds))


# ===== PART 2: The list of problems ================================================================
# WHAT: A list that collects each problem of a plan.
# WHY:  The validator reports all problems together. It does not stop at the first one.


class _Issues(list[ValidationIssue]):
    """WHAT: The list of problems found in one plan."""

    def add(self, path: str, message: str) -> None:
        """WHAT: Adds one problem.

        INPUT:  path - the plan key with the problem. message - the problem in plain English.
        """
        self.append(ValidationIssue(path=path, message=message))

    def field(self, path: str, name: str | None, kinds: tuple[FieldKind, ...], purpose: str) -> bool:
        """WHAT:   Makes sure that a field name is in the field registry and has a permitted kind.
        WHY:    The LLM must not use a field that does not exist.
        INPUT:  path - the plan key. name - the field name. kinds - the permitted kinds.
                purpose - the use of the field, for the message.
        OUTPUT: True if the field is correct. If not, adds a problem and returns False.
        """
        if name not in REGISTRY:
            self.add(path, f'"{name}" is not a known field. Known fields: {_names()}.')
            return False
        if REGISTRY[name].kind not in kinds:
            self.add(path, f'"{name}" cannot be used {purpose}. Fields that can: {_names(*kinds)}.')
            return False
        return True


# ===== PART 3: The main function ===================================================================
# WHAT: Runs all rules on one plan.
# WHY:  This is the only function that other files call.


def validate(plan: QueryPlan) -> list[ValidationIssue]:
    """WHAT:   Returns each problem of the plan.
    WHY:    A plan with a problem must not get data from ClinicalTrials.gov.
    INPUT:  plan - the plan that the LLM wrote.
    OUTPUT: A list of problems. An empty list means that the plan can run.

    If the LLM marked the plan as unsupported, the function does not examine it, because the
    service does not run that plan.
    """
    issues = _Issues()
    if plan.unsupported_reason:
        return issues

    _check_mode_keys(plan, issues)
    {
        PlanMode.AGGREGATE: _check_aggregate,
        PlanMode.HISTOGRAM: _check_histogram,
        PlanMode.POINTS: _check_points,
        PlanMode.NETWORK: _check_network,
    }[plan.mode](plan, issues)
    _check_series(plan, issues)
    _check_years("filters", plan.filters, issues)
    if not 1 <= plan.limit <= MAX_LIMIT:
        issues.add("limit", f"limit must be between 1 and {MAX_LIMIT}, not {plan.limit}.")
    return issues


# ===== PART 4: Rules for each plan mode ============================================================
# WHAT: One function for each plan mode: aggregate, histogram, points, network.
# WHY:  Each mode needs different plan keys.
# All functions in this part have the same INPUT (the plan and the list of problems) and add
# their problems to the list.


def _check_mode_keys(plan: QueryPlan, issues: _Issues) -> None:
    """WHAT: Makes sure that the plan sets no key of a different mode.
    WHY:  A key of a different mode shows that the LLM mixed two modes.
    """
    for key in sorted(_ALL_MODE_KEYS - _MODE_KEYS[plan.mode]):
        if getattr(plan, key) not in (None, []):
            owners = " or ".join(mode.value for mode, keys in _MODE_KEYS.items() if key in keys)
            issues.add(key, f"{key} belongs to {owners} mode; leave it empty in {plan.mode.value} mode.")


def _check_bin(plan: QueryPlan, issues: _Issues) -> None:
    """WHAT: Makes sure that the bin has a number field and a width larger than 0."""
    if plan.bin is None:
        return
    issues.field("bin.field", plan.bin.field, (FieldKind.NUMERIC,), "for bins")
    if plan.bin.bin_width <= 0:
        issues.add("bin.bin_width", f"bin_width must be greater than 0, not {plan.bin.bin_width:g}.")


def _check_aggregate(plan: QueryPlan, issues: _Issues) -> None:
    """WHAT: Examines a plan that counts trials for each bucket.
    WHY:  The mode needs 1 or 2 group_by fields, and a number field needs a bin.
    """
    if not 1 <= len(plan.group_by) <= MAX_GROUP_BY:
        issues.add("group_by", f"aggregate mode needs 1 or {MAX_GROUP_BY} group_by fields, not {len(plan.group_by)}.")
    if len(set(plan.group_by)) < len(plan.group_by):
        issues.add("group_by", "group_by lists the same field twice.")
    numeric = []
    for i, name in enumerate(plan.group_by):
        if issues.field(f"group_by[{i}]", name, _GROUPABLE, "in group_by") and REGISTRY[name].kind is FieldKind.NUMERIC:
            numeric.append(name)
    _check_bin(plan, issues)
    for name in numeric:
        if plan.bin is None or plan.bin.field != name:
            issues.add("bin", f'"{name}" is a number, so grouping by it needs bin = {{"field": "{name}", "bin_width": ...}}.')
    if plan.bin and plan.bin.field in REGISTRY and plan.bin.field not in plan.group_by:
        issues.add("bin.field", f'bin is set for "{plan.bin.field}", which is not in group_by.')
    if len(plan.group_by) == MAX_GROUP_BY and len(plan.series) > 1:
        issues.add(
            "series",
            "Comparing several series across two group_by fields is not supported yet. "
            "Use one group_by field, or a single series.",
        )


def _check_histogram(plan: QueryPlan, issues: _Issues) -> None:
    """WHAT: Examines a plan for a histogram.
    WHY:  The mode needs a bin, and it shows one series only.
    """
    if plan.bin is None:
        issues.add("bin", f"histogram mode needs bin with a numeric field ({_names(FieldKind.NUMERIC)}) and a bin_width.")
    _check_bin(plan, issues)
    if len(plan.series) > 1:
        issues.add(
            "series",
            "A histogram shows one series. To compare distributions, use aggregate mode with the "
            "numeric field in group_by and the same bin.",
        )


def _check_points(plan: QueryPlan, issues: _Issues) -> None:
    """WHAT: Examines a plan that shows one mark for each trial.
    WHY:  The mode needs two numbers (a scatter plot) or two dates (a timeline), not the two together.
    """
    scatter = plan.x_field is not None or plan.y_field is not None
    gantt = plan.start_field is not None or plan.end_field is not None
    if scatter == gantt:
        issues.add(
            "x_field",
            "points mode needs either x_field and y_field (one dot per trial) or start_field and "
            "end_field (one bar per trial), not both and not neither.",
        )
        return
    pairs = (("x_field", "y_field"), (FieldKind.NUMERIC,), "as a numeric axis") if scatter else (
        ("start_field", "end_field"), _DATES, "as a date",
    )
    keys, kinds, purpose = pairs
    for key in keys:
        if getattr(plan, key) is None:
            issues.add(key, f"{key} is missing; {keys[0]} and {keys[1]} are needed together.")
        else:
            issues.field(key, getattr(plan, key), kinds, purpose)
    first, second = getattr(plan, keys[0]), getattr(plan, keys[1])
    if first is not None and first == second:
        issues.add(keys[1], f"{keys[0]} and {keys[1]} are the same field.")
    if gantt and len(plan.series) > 1:
        issues.add("series", "A timeline of trials shows one series, not a comparison.")


def _check_network(plan: QueryPlan, issues: _Issues) -> None:
    """WHAT: Examines a plan for a network graph.
    WHY:  The mode needs two node fields, one series, and a limit on the number of edges.
    """
    if plan.network is None:
        issues.add("network", "network mode needs network with a source_field and a target_field.")
        return
    for key in ("source_field", "target_field"):
        issues.field(f"network.{key}", getattr(plan.network, key), (FieldKind.CATEGORICAL,), "as network nodes")
    if len(plan.series) > 1:
        issues.add("series", "A network shows one series, not a comparison.")
    if not 1 <= plan.network.max_edges <= MAX_EDGES:
        issues.add("network.max_edges", f"max_edges must be between 1 and {MAX_EDGES}, not {plan.network.max_edges}.")


# ===== PART 5: Rules for all plan modes ============================================================
# WHAT: Rules for the series and for the year filters.
# WHY:  All modes use series and filters.


def _check_series(plan: QueryPlan, issues: _Issues) -> None:
    """WHAT: Examines the series: their number, their labels, and their filters.
    WHY:  Two series with the same filters give the same data, so a comparison has no meaning.
    """
    if not 1 <= len(plan.series) <= MAX_SERIES:
        issues.add("series", f"There must be 1 to {MAX_SERIES} series, not {len(plan.series)}.")
    labels = [series.label.strip() for series in plan.series]
    if any(not label for label in labels):
        issues.add("series", "Every series needs a label.")
    if len({label.casefold() for label in labels}) < len(labels):
        issues.add("series", "Two series have the same label.")
    if len(plan.series) > 1:
        if any(not series.filters.model_dump(exclude_none=True) for series in plan.series):
            issues.add("series", "When several series are compared, each one needs its own filter (for example a drug).")
        seen = [series.filters for series in plan.series]
        if any(seen.count(filters) > 1 for filters in seen):
            issues.add("series", "Two series have identical filters, so they would be the same data.")
    for i, series in enumerate(plan.series):
        _check_years(f"series[{i}].filters", series.filters, issues)


def _check_years(path: str, filters: PlanFilters, issues: _Issues) -> None:
    """WHAT:  Makes sure that the year filters are in the permitted range and in the correct order.
    WHY:   The LLM can write a year that is not possible.
    INPUT: path - the plan key of the filters. filters - the filters to examine.
    """
    latest = date.today().year + YEARS_AHEAD
    for key in ("start_year_min", "start_year_max"):
        year = getattr(filters, key)
        if year is not None and not MIN_YEAR <= year <= latest:
            issues.add(f"{path}.{key}", f"{key} must be between {MIN_YEAR} and {latest}, not {year}.")
    if None not in (filters.start_year_min, filters.start_year_max) and filters.start_year_min > filters.start_year_max:
        issues.add(f"{path}.start_year_min", "start_year_min is later than start_year_max.")
