"""TASK:     Runs the steps for one request: plan, validate, fetch, aggregate, cite, build.
GRADING:  System Design. One fixed flow for all questions, and each failure gives a clear response.
INPUT:    A QueryRequest from app/api.py.
OUTPUT:   A VisualizationResponse. This file does not raise an exception to the caller.
USED BY:  app/api.py, evals/run_evals.py
DIAGRAM:  2 (the life of one request) and 8 (from a failure to the next step).

PARTS:
    1. The table of plan modes
    2. Questions to offer when there is no chart
    3. Checks and corrections on a plan
    4. The Pipeline class: start of a request
    5. Make the plan
    6. Run the plan
    7. Offers and fetch failures

Rules:
    - This file has no code for a specific question. The plan mode selects the aggregator
      from the AGGREGATORS table.
    - Each failure becomes an `unsupported` response or an `error` response.
    - The service examines each offered question before it offers it.
"""
import asyncio
import inspect
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from functools import partial

from app import spec_builder
from app.aggregate.aggregate import aggregate
from app.aggregate.histogram import histogram
from app.aggregate.labels import NameLabeler
from app.aggregate.network import network
from app.aggregate.points import points
from app.citations import CITATION_FIELDS, build_citations
from app.ctgov.client import UNAVAILABLE_MESSAGE, CtGovClient, CtGovError, merge_filters, tier2_field
from app.ctgov.normalize import COMBINATIONS, phase_label
from app.models import (
    AggregateResult,
    FetchErrorKind,
    FetchResult,
    PlanDraft,
    PlanFilters,
    PlanMode,
    QueryPlan,
    QueryRequest,
    ResponseStatus,
    RunState,
    SearchTerms,
    Series,
    SeriesFetch,
    Suggestion,
    ValidationIssue,
    VisualizationResponse,
)
from app.planner.planner import Planner, PlannerUnavailable
from app.validator import validate
from app.runlog import RunLog

# ===== PART 1: The table of plan modes ============================================================
# WHAT: Connects each plan mode to the function that counts its trials.
# WHY:  To add a plan mode is one new entry. No other code changes.

# Plan mode -> the function that changes trials into data points. To add a mode, add one entry.
AGGREGATORS: dict[PlanMode, Callable[[list[SeriesFetch], QueryPlan], AggregateResult]] = {
    PlanMode.AGGREGATE: aggregate,
    PlanMode.HISTOGRAM: histogram,
    PlanMode.POINTS: points,
    PlanMode.NETWORK: network,  # This one is async. It gets the name labeler when the pipeline has one.
}


def plan_fields(plan: QueryPlan, with_citations: bool = False) -> list[str]:
    """WHAT:   Returns the fields that this run reads from each trial.
    WHY:    A trial is built with the necessary fields only.
    INPUT:  plan - the plan. with_citations - True if the request wants citations.
    OUTPUT: A list of field names.
    """
    names = [*plan.group_by, plan.x_field, plan.y_field, plan.start_field, plan.end_field]
    if plan.bin:
        names.append(plan.bin.field)
    if plan.network:
        names += [plan.network.source_field, plan.network.target_field]
    if plan.start_field:
        names.append("brief_title")  # The label of a timeline bar is the title of its trial.
    if with_citations:
        names += CITATION_FIELDS
    return list(dict.fromkeys(name for name in names if name))


def phase_filter_note(filters: PlanFilters) -> str | None:
    """WHAT:   Returns the note for a phase filter, or None.
    WHY:    For ClinicalTrials.gov, a Phase 1/2 trial is in Phase 1 and also in Phase 2.
            The service keeps these trials and tells the user.
    INPUT:  filters - the filters of the plan.
    OUTPUT: The note text, or None if there is no phase filter.
    """
    chosen = [phase.value for phase in filters.phase or []]
    spanning = [
        phase_label(list(combination))
        for combination in COMBINATIONS["phase"]
        if 0 < sum(value in chosen for value in combination) < len(combination)
    ]
    if not spanning:
        return None
    names = " and ".join(phase_label([value]) for value in chosen)
    return f"The {names} filter also includes trials listed as {' and '.join(spanning)}."


@contextmanager
def _timed() -> Iterator[Callable[[], float]]:
    """WHAT: Measures the time of one step in milliseconds."""
    started = time.perf_counter()
    yield lambda: (time.perf_counter() - started) * 1000


# ===== PART 2: Questions to offer when there is no chart ==========================================
# WHAT: Makes the list of questions that the user can select after a refusal.
# WHY:  The user must always have a next step.

MAX_ALTERNATIVES = 3
MAX_CANDIDATES = 5  # The number of questions to examine, to get MAX_ALTERNATIVES that work.
# The service offers these when a refused question has no subject.
STARTER_QUESTIONS = (
    "How are melanoma trials distributed across phases?",
    "Which countries have the most recruiting trials for lung cancer?",
    "How has the number of trials for pembrolizumab changed per year since 2015?",
)


def reworded_questions(plan: QueryPlan | None) -> list[Suggestion]:
    """WHAT:   Returns questions to offer when the service cannot answer the question.
    WHY:    The user must always have a question to select.
    INPUT:  plan - the refused plan, or None.
    OUTPUT: A list of suggestions, best first:
                1. The alternatives that the planner wrote.
                2. Three standard questions about the subject in the plan.
                3. The starter questions.
            `Pipeline._answerable` decides which of them the user sees.
    """
    questions = [q.strip() for q in (plan.alternatives if plan else []) if 3 <= len(q.strip()) <= 200]
    if plan is not None:
        filters = [plan.filters, *(series.filters for series in plan.series)]
        subject = next((value for f in filters for value in (f.drug, f.condition, f.sponsor) if value), None)
        if subject:
            questions += [
                f"How are {subject} trials distributed across phases?",
                f"How many {subject} trials started each year?",
                f"Which countries have the most {subject} trials?",
            ]
    questions = list(dict.fromkeys([*questions, *STARTER_QUESTIONS]))
    return [Suggestion(label=question, query=question) for question in questions]


# Plan filter -> its name in an offer of the question without that filter.
_FILTER_WORDS = {
    "condition": "the condition", "drug": "the drug", "sponsor": "the sponsor", "country": "the country",
    "status": "the status", "phase": "the phase", "start_year_min": "the earliest start year",
    "start_year_max": "the latest start year",
}
# ===== PART 3: Checks and corrections on a plan ===================================================
# WHAT: Applies the explicit request fields, and examines the one sentence of the LLM.
# WHY:  Code, not the LLM, has the last decision on the plan.

# Request field -> the plan filter that it sets. An explicit request field has priority over the LLM.
_REQUEST_FILTERS = {
    "sponsor": "sponsor",
    "country": "country",
    "trial_phase": "phase",
    "status": "status",
    "start_year": "start_year_min",
    "end_year": "start_year_max",
}
# Request fields that accept one name or a list. A list of names becomes the comparison series.
_REQUEST_NAMES = {"drug_name": "drug", "condition": "condition"}
BOTH_LISTS_MESSAGE = "Give several drugs or several conditions to compare, not both at once."


def _shown(value: object) -> str:
    """WHAT: Returns a filter value as text for a note."""
    items = value if isinstance(value, list) else [value]
    return ", ".join(str(getattr(item, "value", item)) for item in items)


def _same(a: object, b: object) -> bool:
    """WHAT: Returns True if two filter values are the same. Ignores capital letters and the sequence of a list."""
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if isinstance(a, list) and isinstance(b, list):
        return set(a) == set(b)
    return a == b


_NCT_ID = re.compile(r"NCT\d{8}", re.IGNORECASE)
_NUMBER = re.compile(r"\d+")


def interpretation_is_grounded(text: str, query: str, plan: QueryPlan) -> bool:
    """WHAT:   Returns True if the one sentence of the LLM contains known facts only.
    WHY:    The user sees this sentence. It must not contain an invented number (P1).
    INPUT:  text - the sentence. query - the question. plan - the plan.
    OUTPUT: False if the sentence has a trial ID, or a number that is not in the question or the plan.
    """
    if _NCT_ID.search(text):
        return False
    given = set(_NUMBER.findall(query)) | set(_NUMBER.findall(plan.model_dump_json(exclude={"interpretation"})))
    return all(number in given for number in _NUMBER.findall(text.replace(",", "")))


def canonical(plan: QueryPlan) -> QueryPlan:
    """WHAT:   Moves the filters of a single series into the filters of the plan.
    WHY:    One series is not a comparison. After this step, the plan, the title, and the
            citations are the same for the two forms that the LLM can write.
    INPUT:  plan - the plan.
    OUTPUT: The plan in its standard form.
    """
    if len(plan.series) != 1:
        return plan
    own = plan.series[0].filters.model_dump(exclude_none=True)
    if not own and plan.series[0].label == "All":
        return plan
    return plan.model_copy(update={"filters": plan.filters.model_copy(update=own), "series": [Series(label="All")]})


def apply_request_filters(plan: QueryPlan, request: QueryRequest) -> tuple[QueryPlan, list[str]]:
    """WHAT:   Applies the explicit request fields to a plan.
    WHY:    An explicit field has priority over the value that the LLM read from the question.
    INPUT:  plan - the plan from the LLM. request - the request.
    OUTPUT: (the changed plan, a list of notes). Each replacement of a different value gives a note.
            A list of drug names (or conditions) becomes one series for each name.
    """
    notes: list[str] = []
    filters = plan.filters.model_copy()
    series = [s.model_copy(deep=True) for s in plan.series]

    def replace(request_field: str, key: str, value: object) -> None:
        previous = [getattr(filters, key), *(getattr(s.filters, key) for s in series)]
        differing = [old for old in previous if old is not None and not _same(old, value)]
        if differing:
            notes.append(
                f"The {request_field} given with the request ({_shown(value)}) was used instead of "
                f"{_shown(differing[0])}, which was read from the question."
            )
        setattr(filters, key, value)
        for s in series:
            setattr(s.filters, key, None)

    for request_field, key in _REQUEST_FILTERS.items():
        value = getattr(request, request_field)
        if value is not None:
            replace(request_field, key, value)

    for request_field, key in _REQUEST_NAMES.items():
        names = getattr(request, request_field)
        if not names:
            continue
        if len(names) == 1:
            replace(request_field, key, names[0])
            continue
        planned = {s.label.casefold() for s in series}
        if len(series) > 1 and planned != {name.casefold() for name in names}:
            notes.append(
                f"The groups compared are the {len(names)} names given in {request_field} "
                f"({_shown(names)}), not the ones read from the question."
            )
        setattr(filters, key, None)
        series = [Series(label=name, filters={key: name}) for name in names]

    # If all series are now the same (their different filter was replaced), they become one group.
    if len(series) > 1 and all(s.filters == series[0].filters for s in series):
        series = [Series(label="All")]
    return canonical(plan.model_copy(update={"filters": filters, "series": series})), notes


# ===== PART 4: The Pipeline class: start of a request =============================================
# WHAT: The entry point for one request.
# WHY:  All requests go through the same steps.


class Pipeline:
    """WHAT: Runs one request from the question to the response."""

    def __init__(
        self,
        client: CtGovClient,
        planner: Planner | None = None,
        labeler: NameLabeler | None = None,
        run_log: RunLog | None = None,
        check_suggestions: bool = False,
    ) -> None:
        """WHAT:  Connects the pipeline to its parts.
        INPUT: client - the registry client. planner - the LLM planner (optional).
               labeler - the label model for networks (optional). run_log - the text log (optional).
               check_suggestions - True to examine each offered question with the LLM.
        """
        self._client = client
        self._planner = planner
        self._run_log = run_log
        self._check_suggestions = check_suggestions
        self._aggregators = dict(AGGREGATORS)
        if labeler is not None and PlanMode.NETWORK in self._aggregators:
            self._aggregators[PlanMode.NETWORK] = partial(self._aggregators[PlanMode.NETWORK], labeler=labeler)

    async def run(self, request: QueryRequest) -> VisualizationResponse:
        """WHAT:   Answers a question: the LLM makes a plan, code examines it, then code runs it.
        INPUT:  request - the request.
        OUTPUT: The response. The status is ok, unsupported, or error.
        """
        state = RunState(request=request)
        if self._run_log is None:
            return await self._guarded(state, self._plan_and_execute(state))
        self._run_log.start(request)
        response = await self._guarded(state, self._plan_and_execute(state))
        self._run_log.finish(state, response)
        return response

    async def run_plan(self, request: QueryRequest, plan: QueryPlan) -> VisualizationResponse:
        """WHAT:   Runs a plan that a person wrote. Does not use an LLM.
        WHY:    Tests and examples can run the code steps without the LLM.
        INPUT:  request - the request. plan - the plan.
        OUTPUT: The response.
        """
        state = RunState(request=request, plan=plan)
        return await self._guarded(state, self._execute(state, plan))

    @staticmethod
    async def _guarded(state: RunState, work) -> VisualizationResponse:
        """WHAT: Runs the work and changes an unexpected exception into an `error` response.
        WHY:  The API layer must always get a response object.
        """
        try:
            return await work
        except Exception as exc:  # The API layer must always get a response object.
            state.record("failed", 0.0, error=f"{type(exc).__name__}: {exc}")
            return spec_builder.failure_response(
                state, ResponseStatus.ERROR, "internal_error", "Something went wrong while building the chart."
            )


    # ===== PART 5: Make the plan ==================================================================
    # WHAT: Gets a plan from the LLM and validates it. Permits one retry.
    # WHY:  A plan that is not valid must not get data.

    async def _plan_and_execute(self, state: RunState) -> VisualizationResponse:
        """WHAT: Makes the plan, then runs it. Returns the refusal if there is no valid plan."""
        outcome = await self.plan(state)
        if isinstance(outcome, VisualizationResponse):
            return outcome
        return await self._execute(state, outcome)

    async def plan(self, state: RunState) -> QueryPlan | VisualizationResponse:
        """WHAT:   Changes the request into a validated plan, or into a response that gives the cause.
        WHY:    This step is separate from the run, so that the evaluation can give a score to
                planning and fetch no trials.
        INPUT:  state - the state of this request.
        OUTPUT: A valid QueryPlan, or a response with status unsupported or error.

        Sequence: planner draft -> explicit request fields -> validation.
        If the first draft is not valid, the planner writes one corrected draft (P2).
        """
        request = state.request
        if all(len(getattr(request, name) or []) > 1 for name in _REQUEST_NAMES):
            return spec_builder.failure_response(state, ResponseStatus.UNSUPPORTED, "invalid_request", BOTH_LISTS_MESSAGE)
        if self._planner is None:
            return self._llm_failure(state, "No language model is configured.")

        draft = PlanDraft()
        plan: QueryPlan | None = None
        issues: list[ValidationIssue] = []
        notes: list[str] = []
        for attempt in (0, 1):  # The first plan, then a maximum of one corrected plan (P2).
            before = draft
            with _timed() as elapsed:
                try:
                    draft = await (self._planner.draft(request) if attempt == 0 else self._planner.revise(draft, issues))
                except PlannerUnavailable as exc:
                    state.record("plan", elapsed(), retries=attempt, error=str(exc))
                    return self._llm_failure(state, str(exc))
            # A retry continues the same conversation, so the last draft contains all of it.
            state.llm_exchanges = [{
                "purpose": "write the query plan", "model": draft.model, "conversation": draft.conversation,
                "used": "parsed into the plan" if draft.plan is not None else f"not usable: {draft.parse_error}",
            }]
            state.record(
                "plan",
                elapsed(),
                model=draft.model,
                retries=attempt,
                llm_calls=draft.llm_calls - before.llm_calls,
                tool_calls=[call.model_dump() for call in draft.tool_calls[len(before.tool_calls):]],
                input_tokens=draft.input_tokens - before.input_tokens,
                output_tokens=draft.output_tokens - before.output_tokens,
            )
            with _timed() as elapsed:
                if draft.plan is None:
                    plan, notes, issues = None, [], [ValidationIssue(path="", message=draft.parse_error or "No plan.")]
                else:
                    plan, notes = apply_request_filters(draft.plan, request)
                    issues = validate(plan)
            state.record("validate", elapsed(), errors=[issue.model_dump() for issue in issues])
            if not issues:
                break

        if issues or plan is None:
            return spec_builder.failure_response(
                state,
                ResponseStatus.UNSUPPORTED,
                "plan_invalid",
                "This question could not be turned into a query that can be run. Try rephrasing it, "
                "or name a drug, condition or sponsor.",
                [issue.message for issue in issues],
                suggestions=await self._answerable(reworded_questions(plan)),
            )

        # Code replaces the interpretation with the title when it contains a number or a trial
        # ID that the LLM did not get, or when request fields changed its subject.
        if not plan.unsupported_reason and (notes or not interpretation_is_grounded(plan.interpretation, request.query, plan)):
            state.record("interpretation", 0.0, replaced=True, original=plan.interpretation)
            plan = plan.model_copy(update={"interpretation": spec_builder.build_title(plan) + "."})
        # `search_terms` is for terms that are different from the typed text. The model
        # sometimes lists the word of the user there. That would show a correction that did not occur.
        explicit = request.model_dump(mode="json", include={"drug_name", "condition", "sponsor"}, exclude_none=True)
        typed = " ".join([request.query, *(name for value in explicit.values() for name in ([value] if isinstance(value, str) else value))]).casefold()
        corrections = {
            key: [term for term in terms if term.casefold() not in typed]
            for key, terms in plan.search_terms.model_dump().items()
        }
        plan = plan.model_copy(update={"search_terms": SearchTerms(**corrections)})
        state.plan = plan
        for note in notes:
            state.note(note)
        for key, terms in plan.search_terms.model_dump(exclude_defaults=True).items():
            state.note(f"{key.capitalize()} searched as: {', '.join(terms).rstrip('.')}.")
        return plan

    @staticmethod
    def _llm_failure(state: RunState, reason: str) -> VisualizationResponse:
        """WHAT: Returns the `error` response for an LLM that is not available."""
        return spec_builder.failure_response(
            state, ResponseStatus.ERROR, "llm_unavailable", f"The question could not be interpreted: {reason}"
        )


    # ===== PART 6: Run the plan ===================================================================
    # WHAT: Fetches the trials, counts them, makes citations, and builds the chart.
    # WHY:  These steps use code only. The LLM is not used here.

    async def _execute(self, state: RunState, plan: QueryPlan) -> VisualizationResponse:
        """WHAT:   Runs a valid plan: fetch, aggregate, cite, build.
        WHY:    These steps compute each number in the response. The LLM is not used.
        INPUT:  state - the state of this request. plan - the valid plan.
        OUTPUT: The response.
        """
        if plan.unsupported_reason:
            return spec_builder.failure_response(
                state, ResponseStatus.UNSUPPORTED, "unsupported_question", plan.unsupported_reason,
                suggestions=await self._answerable(reworded_questions(plan)),
            )
        if plan.mode not in self._aggregators:
            return spec_builder.failure_response(
                state, ResponseStatus.UNSUPPORTED, "mode_not_available", f"{plan.mode.value} charts are not available yet."
            )

        for series in plan.series:
            note = phase_filter_note(merge_filters(plan.filters, series.filters))
            if note:
                state.note(note)

        with _timed() as elapsed:
            fetch = await self._client.fetch(plan, plan_fields(plan, state.request.max_citations > 0))
        state.record(
            "fetch",
            elapsed(),
            api_calls=fetch.api_calls,
            cache_hits=fetch.cache_hits,
            tier=max((s.tier for s in fetch.series if s.tier), default=None),
            trials_matched=sum(s.trials_matched for s in fetch.series),
            series=[
                {"label": s.label, "trials_matched": s.trials_matched, "tier": s.tier, "error": s.error_detail}
                for s in fetch.series
            ],
        )
        failure = self._fetch_failure(state, fetch)
        if failure and failure.error.code == "too_many_trials":
            with _timed() as elapsed:
                failure.error.suggestions = await self._narrower_versions(fetch)
            state.record("suggest", elapsed(), offered=len(failure.error.suggestions))
            failure.meta.trace = list(state.trace) if state.request.debug else None
        if failure:
            return failure

        with _timed() as elapsed:
            result = self._aggregators[plan.mode](fetch.series, plan)
            if inspect.isawaitable(result):
                result = await result
        state.llm_exchanges += result.llm_exchanges
        state.record(
            "aggregate", elapsed(), data_points=len(result.about), trials_used=result.trials_used,
            dropped=result.dropped, **result.trace,
        )

        with _timed() as elapsed:
            citations = build_citations(result, fetch.series, plan, state.request.max_citations)
        state.record(
            "cite",
            elapsed(),
            data_points=len(citations),
            trials_cited=sum(c.shown for c in citations.values()),
            from_sample=any(c.is_sample for c in citations.values()),
        )

        with _timed() as elapsed:
            suggested = state.request.chart_type or plan.chart_type
            chart, override_note = spec_builder.choose_chart(plan, result, suggested)
            if override_note:
                state.note(override_note)
            visualization = spec_builder.build_visualization(plan, result, chart)
        state.record("build", elapsed(), chart=chart.value, chart_overridden=override_note is not None)

        response = spec_builder.build_response(state, fetch, result, visualization, citations, override_note is not None)
        if response.meta.trials_matched == 0:
            with _timed() as elapsed:
                response.meta.suggestions = await self._wider_versions(plan)
            state.record("suggest", elapsed(), offered=len(response.meta.suggestions))
            response.meta.trace = list(state.trace) if state.request.debug else None
        return response

    # ===== PART 7: Offers and fetch failures ======================================================
    # WHAT: Examines each offer, and makes the response when the registry fails.
    # WHY:  An offer that fails is worse than no offer.

    async def _answerable(self, candidates: list[Suggestion]) -> list[Suggestion]:
        """WHAT:   Returns the first candidates that give a chart when the user selects them.
        WHY:    An offered question that fails is worse than no offer. A statement from the LLM
                that a question "can be answered" is not proof (P2).
        INPUT:  candidates - the questions to examine.
        OUTPUT: A maximum of 3 suggestions, each with its real trial count.

        The service keeps a candidate only if it gives a valid plan, finds one trial or more, and
        is not larger than the size limit. The checks run in parallel.
        """
        if not self._check_suggestions:
            return candidates[:MAX_ALTERNATIVES]
        checked = await asyncio.gather(*(self._check(candidate) for candidate in candidates[:MAX_CANDIDATES]))
        good = [candidate for candidate in checked if candidate is not None]
        # No candidate passed. The model or the registry can be the cause. Offer the starter questions.
        return good[:MAX_ALTERNATIVES] or [Suggestion(label=q, query=q) for q in STARTER_QUESTIONS]

    async def _wider_versions(self, plan: QueryPlan) -> list[Suggestion]:
        """WHAT:   Returns wider questions for a question that found no trials.
        WHY:    The user can get a result with one click.
        INPUT:  plan - the plan that found no trials.
        OUTPUT: Suggestions. Each one is the same question without one filter, with its real count.
                If the plan has one filter only, the output is the starter questions.

        The service does not remove a filter itself (P4). These are offers.
        """
        filters = plan.filters
        active = [name for name, value in filters.model_dump().items() if value not in (None, [], "")]
        candidates: list[Suggestion] = []
        if len(plan.series) == 1 and len(active) > 1:
            wider = [plan.model_copy(update={"filters": filters.model_copy(update={name: None})}) for name in active]
            try:
                counts = await asyncio.gather(*(self._client.count(p.filters) for p in wider))
            except CtGovError:
                counts = [0] * len(wider)
            for name, wide, count in zip(active, wider, counts):
                if count and (count <= self._client.max_trials or tier2_field(wide)):
                    question = spec_builder.build_title(wide)
                    label = f"{question} (without {_FILTER_WORDS[name]})"
                    candidates.append(Suggestion(label=label, query=question, trials_matched=count))
        if self._check_suggestions and candidates:
            checked = await asyncio.gather(*(self._check(c) for c in candidates[:MAX_CANDIDATES]))
            candidates = [c.model_copy(update={"label": o.label}) for o, c in zip(candidates, checked) if c is not None]
        return candidates[:MAX_ALTERNATIVES] or [Suggestion(label=q, query=q) for q in STARTER_QUESTIONS]

    async def _check(self, candidate: Suggestion) -> Suggestion | None:
        """WHAT:   Examines one offered question: plans it, validates it, and counts its trials.
        OUTPUT: The suggestion with its trial count, or None if it must not be offered.
        """
        try:
            plan = await self.plan(RunState(request=QueryRequest(query=candidate.query)))
            if not isinstance(plan, QueryPlan) or plan.unsupported_reason:
                return None
            counts = await asyncio.gather(
                *(self._client.count(merge_filters(plan.filters, series.filters)) for series in plan.series)
            )
        except Exception:  # If the check cannot run, the candidate is not offered.
            return None
        too_big = any(count > self._client.max_trials for count in counts) and tier2_field(plan) is None
        if too_big or not all(counts):
            return None
        return candidate.model_copy(update={"trials_matched": sum(counts)})

    async def _narrower_versions(self, fetch: FetchResult) -> list[Suggestion]:
        """WHAT:   Returns narrower versions of a question that found too many trials.
        WHY:    The user can get a result with one click.
        INPUT:  fetch - the result of the fetch step.
        OUTPUT: Suggestions with real trial counts. Each adds one filter: a status, a phase, or a
                recent start year. A filter type that the user already set is not used.

        These are offers only. The user decides to add a filter, not the service (P4).
        """
        too_big = [s for s in fetch.series if s.error_kind is FetchErrorKind.OVER_CAP]
        this_year = date.today().year
        options = [
            ("Recruiting trials only", {"status": ["RECRUITING"]}, {"status": ["RECRUITING"]}),
            ("Completed trials only", {"status": ["COMPLETED"]}, {"status": ["COMPLETED"]}),
            ("Phase 3 trials only", {"trial_phase": ["PHASE3"]}, {"phase": ["PHASE3"]}),
            ("Phase 2 trials only", {"trial_phase": ["PHASE2"]}, {"phase": ["PHASE2"]}),
            (f"Trials started since {this_year - 5}", {"start_year": this_year - 5}, {"start_year_min": this_year - 5}),
            (f"Trials started since {this_year - 10}", {"start_year": this_year - 10}, {"start_year_min": this_year - 10}),
        ]
        used = {key for s in too_big for key, value in s.filters.model_dump(exclude_none=True).items() if value != []}
        options = [option for option in options if not set(option[2]) & used and not ("start_year_min" in option[2] and "start_year_max" in used)]

        async def largest(extra: dict) -> int:
            counts = await asyncio.gather(*(self._client.count(s.filters.model_copy(update=PlanFilters(**extra).model_dump(exclude_none=True))) for s in too_big))
            return max(counts)

        try:
            sizes = await asyncio.gather(*(largest(extra) for _, _, extra in options))
        except CtGovError:
            return []  # The refusal is complete without suggestions. Suggestions are an aid only.
        limit = self._client.max_trials
        return [
            Suggestion(label=label, request=request, trials_matched=size)
            for (label, request, _), size in zip(options, sizes)
            if 0 < size <= limit
        ]

    @staticmethod
    def _fetch_failure(state: RunState, fetch: FetchResult) -> VisualizationResponse | None:
        """WHAT:   Decides if the fetch result can be used.
        INPUT:  state - the state of this request. fetch - the result of the fetch step.
        OUTPUT: None if one series or more arrived. Each missing series becomes a note.
                If no series arrived, the failure response.
        """
        failed = [s for s in fetch.series if s.error]
        if len(failed) < len(fetch.series):
            for s in failed:
                state.note(f'The "{s.label}" series could not be loaded and is left out: {s.error}')
            return None
        first = failed[0]
        details = [f"{s.label}: {s.error}" for s in failed] if len(failed) > 1 else []
        if first.error_kind is FetchErrorKind.OVER_CAP:
            return spec_builder.failure_response(
                state, ResponseStatus.UNSUPPORTED, "too_many_trials", first.error, details, fetch
            )
        if first.error_kind is FetchErrorKind.REJECTED:
            return spec_builder.failure_response(
                state, ResponseStatus.ERROR, "request_rejected",
                "ClinicalTrials.gov did not accept this search.", [first.error], fetch,
            )
        return spec_builder.failure_response(
            state, ResponseStatus.ERROR, "ctgov_unavailable", UNAVAILABLE_MESSAGE, details, fetch
        )
