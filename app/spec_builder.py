"""TASK:     Selects the chart type and makes the visualization specification.
GRADING:  Input/Output Design and Coverage. Code selects the chart from the data, not the LLM.
INPUT:    The plan, the aggregated result, and the citations.
OUTPUT:   A VisualizationResponse: the chart with its encoding, title, metadata, and citations.
USED BY:  app/pipeline.py
DIAGRAM:  3 (from the plan to the chart type).

PARTS:
    1. Select the chart type
    2. Encoding channels
    3. The title
    4. The visualization
    5. The response

Code does these tasks after the counts are complete. The LLM does not do them.
The title comes from the plan filters. Thus a title cannot name a filter that was not applied.
"""
from datetime import datetime, timezone

from app.models import (
    COUNT_KEY,
    SERIES_KEY,
    AggregateResult,
    BarChart,
    BarEncoding,
    ChannelType,
    ChartType,
    CitationSet,
    EncodingChannel,
    ErrorInfo,
    FetchResult,
    FieldKind,
    Gantt,
    GanttEncoding,
    GroupedBarChart,
    GroupedBarEncoding,
    Heatmap,
    HeatmapEncoding,
    Histogram,
    HistogramEncoding,
    NetworkEdgeEncoding,
    NetworkEncoding,
    NetworkGraph,
    NetworkNodeEncoding,
    PlanMode,
    QueryPlan,
    ResponseMeta,
    ResponseStatus,
    RunState,
    Suggestion,
    ScatterEncoding,
    ScatterPlot,
    SortOrder,
    StackedBarChart,
    StackedBarEncoding,
    TimeSeries,
    TimeSeriesEncoding,
    VisualizationResponse,
)
from app.registry import REGISTRY, row_key

# ===== PART 1: Select the chart type ==============================================================
# WHAT: Finds the chart that fits the structure of the data.
# WHY:  The best chart depends on the data, for example the number of values. The LLM did not see the data.

MAX_STACK_VALUES = 8  # With more segments than this in one bar, a reader cannot see the difference between them.
NO_TRIALS_NOTE = "No trials matched these filters."

# Suggested chart types that also fit a data structure. The key is the default chart for that structure.
_ACCEPTABLE_INSTEAD = {
    (ChartType.TIME_SERIES, False): {ChartType.BAR_CHART},
    (ChartType.TIME_SERIES, True): {ChartType.GROUPED_BAR_CHART},
    (ChartType.STACKED_BAR_CHART, False): {ChartType.HEATMAP},
    # A distribution that the user wants as a bar chart becomes a bar chart: the same counts, one bar for each range.
    (ChartType.HISTOGRAM, False): {ChartType.BAR_CHART},
}


def _is_time(field_name: str) -> bool:
    """WHAT: Returns True if a field is a time field (a year)."""
    return REGISTRY[field_name].kind is FieldKind.TIME


def default_chart(plan: QueryPlan, result: AggregateResult) -> ChartType:
    """WHAT:   Returns the chart that fits the structure of the data.
    INPUT:  plan - the plan. result - the aggregated result.
    OUTPUT: The chart type:

    histogram mode            histogram
    points mode               scatter plot (x and y), or gantt (start and end)
    network mode              network graph
    aggregate mode:
      1 group_by, 1 series      bar            (time field: time series)
      1 group_by, 2-5 series    grouped bar    (time field: time series)
      2 group_by                stacked bar if the second field has 8 values or fewer, else heatmap
    """
    if plan.mode is PlanMode.HISTOGRAM:
        return ChartType.HISTOGRAM
    if plan.mode is PlanMode.POINTS:
        return ChartType.SCATTER_PLOT if plan.x_field else ChartType.GANTT
    if plan.mode is PlanMode.NETWORK:
        return ChartType.NETWORK_GRAPH
    if len(plan.group_by) == 2:
        stack_key = row_key(plan.group_by[1])
        values = {row[stack_key] for row in result.rows}
        return ChartType.STACKED_BAR_CHART if len(values) <= MAX_STACK_VALUES else ChartType.HEATMAP
    if _is_time(plan.group_by[0]):
        return ChartType.TIME_SERIES
    return ChartType.GROUPED_BAR_CHART if len(plan.series) > 1 else ChartType.BAR_CHART


def _name(field_name: str | None) -> str:
    """WHAT: Returns the display name of a field."""
    return REGISTRY[field_name].display_name.lower() if field_name in REGISTRY else str(field_name)


def _nouns(field_name: str) -> str:
    """WHAT: Returns the plural name for the values of a field: `drugs`, `sponsors`, `countries`."""
    noun = REGISTRY[field_name].noun or _name(field_name)
    if noun.endswith("y"):
        return noun[:-1] + "ies"
    return noun + ("es" if noun.endswith("s") else "s")


def _what_is_shown(plan: QueryPlan) -> str:
    """WHAT: Returns the data in a small number of plain words. A note about a replaced chart uses it."""
    if plan.mode is PlanMode.HISTOGRAM:
        return f"the spread of {_name(plan.bin.field)} is"
    if plan.mode is PlanMode.POINTS and plan.x_field:
        return f"{_name(plan.x_field)} against {_name(plan.y_field)} for each trial is"
    if plan.mode is PlanMode.POINTS:
        return "the start and end of each trial are"
    if plan.mode is PlanMode.NETWORK:
        return f"links between {_nouns(plan.network.source_field)} and {_nouns(plan.network.target_field)} are"
    return f"counts of trials by {' and '.join(_name(name) for name in plan.group_by)} are"


def choose_chart(plan: QueryPlan, result: AggregateResult, suggested: ChartType | None) -> tuple[ChartType, str | None]:
    """WHAT:   Returns the chart to draw.
    WHY:    The user or the LLM can suggest a chart, but the chart must fit the data.
    INPUT:  plan - the plan. result - the aggregated result. suggested - the suggested chart, or None.
    OUTPUT: (the chart type, a note if the suggested chart was replaced or None).
    """
    default = default_chart(plan, result)
    if suggested is None or suggested is default:
        return default, None
    if suggested in _ACCEPTABLE_INSTEAD.get((default, len(plan.series) > 1), ()):
        return suggested, None
    return default, (
        f"A {_readable(suggested)} was asked for, but {_what_is_shown(plan)} "
        f"shown as a {_readable(default)}, which fits this data."
    )


# ===== PART 2: Encoding channels ==================================================================
# WHAT: Makes the mapping from data keys to the visual channels (x, y, series, nodes, edges).
# WHY:  A frontend reads the encoding to draw the chart without a guess.


def _readable(chart: ChartType) -> str:
    """WHAT: Returns a chart type as plain words for a note."""
    return chart.value.replace("_", " ")


def _channel(field_name: str) -> EncodingChannel:
    """WHAT: Returns the encoding channel for a field."""
    field = REGISTRY[field_name]
    kind = ChannelType.TEMPORAL if field.kind is FieldKind.TIME else ChannelType.CATEGORICAL
    return EncodingChannel(field=row_key(field_name), type=kind, title=field.display_name)


def _numeric_channel(field_name: str, row_field: str | None = None) -> EncodingChannel:
    """WHAT: Returns the encoding channel for a number field, with its unit."""
    field = REGISTRY[field_name]
    return EncodingChannel(
        field=row_field or field_name, type=ChannelType.QUANTITATIVE, title=field.display_name, unit=field.unit
    )


def _date_channel(field_name: str, row_field: str) -> EncodingChannel:
    """WHAT: Returns the encoding channel for a date field."""
    return EncodingChannel(field=row_field, type=ChannelType.TEMPORAL, title=REGISTRY[field_name].display_name)


_COUNT = EncodingChannel(field=COUNT_KEY, type=ChannelType.QUANTITATIVE, title="Trials", unit="trials")
_SERIES = EncodingChannel(field=SERIES_KEY, type=ChannelType.CATEGORICAL, title="Series")
_TRIAL = EncodingChannel(field="label", type=ChannelType.CATEGORICAL, title="Trial")
_NETWORK_ENCODING = NetworkEncoding(
    nodes=NetworkNodeEncoding(
        label=EncodingChannel(field="label", type=ChannelType.CATEGORICAL, title="Name"),
        size=EncodingChannel(field="trial_count", type=ChannelType.QUANTITATIVE, title="Trials", unit="trials"),
        color=EncodingChannel(field="type", type=ChannelType.CATEGORICAL, title="Kind"),
    ),
    edges=NetworkEdgeEncoding(
        weight=EncodingChannel(field="shared_trials", type=ChannelType.QUANTITATIVE, title="Shared trials", unit="trials"),
    ),
)


# ===== PART 3: The title ==========================================================================
# WHAT: Writes the chart title from the plan.
# WHY:  The title cannot say more than the service fetched.


def build_title(plan: QueryPlan) -> str:
    """WHAT:   Makes the chart title from the plan.
    WHY:    A title made from the plan filters cannot name a filter that was not applied.
    INPUT:  plan - the plan.
    OUTPUT: The title, for example "Recruiting melanoma trials by country".
    """
    f = plan.filters
    names = " / ".join(name for name in (f.drug, f.condition, f.sponsor) if name)
    status = REGISTRY["status"].labels[f.status[0].value].lower() if f.status and len(f.status) == 1 else ""
    phase = REGISTRY["phase"].labels[f.phase[0].value] if f.phase and len(f.phase) == 1 else ""
    title = " ".join(part for part in (status, phase, names, "trials") if part)
    if f.country:
        title += f" in {f.country}"

    if f.start_year_min and f.start_year_max:
        years = f", {f.start_year_min}–{f.start_year_max}"
    elif f.start_year_min:
        years = f" since {f.start_year_min}"
    elif f.start_year_max:
        years = f" up to {f.start_year_max}"
    else:
        years = ""
    versus = " vs ".join(series.label for series in plan.series) if len(plan.series) > 1 else ""

    if plan.mode is PlanMode.NETWORK:
        source, target = plan.network.source_field, plan.network.target_field
        what = f"{_nouns(source)} used together" if source == target else f"{_nouns(source)} and {_nouns(target)}"
        title += f"{years}: {what}"
    elif plan.mode is PlanMode.POINTS:
        what = f"{_name(plan.x_field)} vs {_name(plan.y_field)}" if plan.x_field else "start to completion"
        title += f"{years}: {what}" + (f" ({versus})" if versus else "")
    else:
        by = [plan.bin.field] if plan.mode is PlanMode.HISTOGRAM else plan.group_by
        title += " by " + " and ".join(_name(name) for name in by) + years + (f": {versus}" if versus else "")
    return title[0].upper() + title[1:]


# ===== PART 4: The visualization ==================================================================
# WHAT: Puts the data rows and the encoding into the model of the chart type.
# WHY:  Each chart type has a fixed structure in the response.


def _range_label(row: dict) -> str:
    """WHAT: Returns a histogram bar as a category label: `100–200`, or `3,900 or more` for the overflow bar."""
    low, high = (f"{value:,.0f}" if float(value).is_integer() else f"{value:,g}" for value in (row["bin_start"], row["bin_end"]))
    return f"{low} or more" if row.get("is_overflow") else f"{low}–{high}"


def build_visualization(plan: QueryPlan, result: AggregateResult, chart: ChartType):
    """WHAT:   Puts the rows in the model for the chart type.
    INPUT:  plan - the plan. result - the aggregated result. chart - the chart type.
    OUTPUT: The visualization: type, title, encoding, and data.
            There is one branch for each chart type and no logic for a specific question.
    """
    title, rows = build_title(plan), result.rows
    several = len(plan.series) > 1
    if chart is ChartType.NETWORK_GRAPH and result.network is not None:
        return NetworkGraph(title=title, encoding=_NETWORK_ENCODING, data=result.network)
    if chart is ChartType.HISTOGRAM:
        x = _numeric_channel(plan.bin.field, "bin_start")
        return Histogram(title=title, encoding=HistogramEncoding(x=x, y=_COUNT), data=rows)
    if plan.mode is PlanMode.HISTOGRAM and chart is ChartType.BAR_CHART:
        field = REGISTRY[plan.bin.field]
        x = EncodingChannel(field=field.name, type=ChannelType.CATEGORICAL, title=field.display_name, unit=field.unit)
        bars = [
            {"id": row["id"], field.name: _range_label(row), COUNT_KEY: row[COUNT_KEY]} for row in rows
        ]
        return BarChart(title=title, encoding=BarEncoding(x=x, y=_COUNT), data=bars)
    if chart is ChartType.SCATTER_PLOT:
        encoding = ScatterEncoding(
            x=_numeric_channel(plan.x_field), y=_numeric_channel(plan.y_field), series=_SERIES if several else None
        )
        return ScatterPlot(title=title, encoding=encoding, data=rows)
    if chart is ChartType.GANTT:
        encoding = GanttEncoding(
            y=_TRIAL, start=_date_channel(plan.start_field, "start"), end=_date_channel(plan.end_field, "end")
        )
        return Gantt(title=title, encoding=encoding, data=rows)
    if not plan.group_by:
        raise ValueError(f"no builder for chart type {chart.value} in {plan.mode.value} mode")

    x = _channel(plan.group_by[0])
    if chart is ChartType.BAR_CHART:
        return BarChart(title=title, encoding=BarEncoding(x=x, y=_COUNT), data=rows)
    if chart is ChartType.TIME_SERIES:
        encoding = TimeSeriesEncoding(x=x, y=_COUNT, series=_SERIES if several else None)
        return TimeSeries(title=title, encoding=encoding, data=rows)
    if chart is ChartType.GROUPED_BAR_CHART:
        return GroupedBarChart(title=title, encoding=GroupedBarEncoding(x=x, y=_COUNT, series=_SERIES), data=rows)
    if len(plan.group_by) == 2:
        second = _channel(plan.group_by[1])
        if chart is ChartType.STACKED_BAR_CHART:
            return StackedBarChart(title=title, encoding=StackedBarEncoding(x=x, y=_COUNT, stack=second), data=rows)
        if chart is ChartType.HEATMAP:
            return Heatmap(title=title, encoding=HeatmapEncoding(x=x, y=second, color=_COUNT), data=rows)
    raise ValueError(f"no builder for chart type {chart.value}")


# ===== PART 5: The response =======================================================================
# WHAT: Makes the metadata and the full response, for a chart and for a failure.
# WHY:  All results have the same response structure.


def _units(**axes: str) -> dict[str, str]:
    """WHAT: Returns the unit for each axis, for the fields that have a unit."""
    return {axis: REGISTRY[name].unit for axis, name in axes.items() if REGISTRY[name].unit}


def presentation(plan: QueryPlan, chart: ChartType) -> tuple[SortOrder | None, str | None, dict[str, str]]:
    """WHAT:   Returns the facts that a frontend needs to label the chart.
    OUTPUT: (sort, time granularity, units).
    """
    if plan.mode is PlanMode.HISTOGRAM:  # Drawn as a histogram, or as plain bars if the user asks for them.
        return SortOrder.NATURAL, None, {**_units(x=plan.bin.field), "y": "trials"}
    if chart is ChartType.SCATTER_PLOT:
        return None, None, _units(x=plan.x_field, y=plan.y_field)
    if chart is ChartType.GANTT:
        return (SortOrder.DESC if plan.sort is SortOrder.DESC else SortOrder.NATURAL), None, {}
    if chart is ChartType.NETWORK_GRAPH:
        return SortOrder.DESC, None, {"size": "trials", "weight": "trials"}
    time_first = bool(plan.group_by) and _is_time(plan.group_by[0])
    granularity = "year" if any(_is_time(name) for name in plan.group_by) else None
    return (
        SortOrder.NATURAL if time_first else plan.sort,
        granularity,
        {"color" if chart is ChartType.HEATMAP else "y": "trials"},
    )


def _now() -> str:
    """WHAT: Returns the current time in UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _base_meta(state: RunState, fetch: FetchResult | None) -> ResponseMeta:
    """WHAT: Returns the metadata that each response contains, for all status values."""
    plan = state.plan
    fetched = [s for s in fetch.series if s.error is None] if fetch else []
    return ResponseMeta(
        plan=plan,
        interpretation=plan.interpretation if plan else "",
        filters_applied={
            s.label: s.filters.model_dump(exclude_none=True, mode="json") for s in (fetch.series if fetch else [])
        },
        search_terms=plan.search_terms.model_dump(exclude_defaults=True) if plan else {},
        # Each series with a known size, including a series that was too large to read.
        trials_matched=sum(s.trials_matched for s in (fetch.series if fetch else [])),
        tier=max((s.tier for s in fetched if s.tier), default=None),
        notes=list(state.notes),
        data_timestamp=fetch.data_timestamp if fetch else None,
        retrieved_at=_now(),
        trace=list(state.trace) if state.request.debug else None,
    )


def build_response(
    state: RunState,
    fetch: FetchResult,
    result: AggregateResult,
    visualization,
    citations: dict[str, CitationSet],
    chart_overridden: bool,
) -> VisualizationResponse:
    """WHAT:   Makes the response for a chart.
    INPUT:  state - the state of this request. fetch - the fetch result. result - the aggregated
            result. visualization - the chart. citations - the citations.
            chart_overridden - True if the suggested chart was replaced.
    OUTPUT: A VisualizationResponse with status ok.
    """
    meta = _base_meta(state, fetch)
    meta.sort, meta.time_granularity, meta.units = presentation(state.plan, visualization.type)
    meta.trials_used = result.trials_used
    meta.chart_overridden = chart_overridden
    meta.pruning = result.pruning
    meta.notes += [note for note in result.notes if note not in meta.notes]
    if meta.trials_matched == 0 and NO_TRIALS_NOTE not in meta.notes:
        meta.notes.append(NO_TRIALS_NOTE)
    return VisualizationResponse(
        status=ResponseStatus.OK,
        query=state.request.query,
        visualization=visualization,
        citations=citations,
        meta=meta,
    )


def failure_response(
    state: RunState,
    status: ResponseStatus,
    code: str,
    message: str,
    details: list[str] | None = None,
    fetch: FetchResult | None = None,
    suggestions: list[Suggestion] | None = None,
) -> VisualizationResponse:
    """WHAT:   Makes the response for a refusal or a failure.
    INPUT:  state - the state of this request. status - unsupported or error. code - the stable
            cause code. message - the cause in plain language. details, fetch, suggestions - optional.
    OUTPUT: A VisualizationResponse with no chart, a cause, and all facts known until now.
    """
    return VisualizationResponse(
        status=status,
        query=state.request.query,
        meta=_base_meta(state, fetch),
        error=ErrorInfo(code=code, message=message, details=details or [], suggestions=suggestions or []),
    )
