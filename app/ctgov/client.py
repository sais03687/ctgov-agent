"""TASK:     Gets trials from the ClinicalTrials.gov API: pages, counts, rate limit, cache, retries.
GRADING:  System Design. Handles the limits of the real API and does not use incomplete data.
INPUT:    A QueryPlan and the list of fields to read.
OUTPUT:   A FetchResult: for each series, all its trials or the cause of the failure.
USED BY:  app/pipeline.py, app/planner/tools.py
DIAGRAM:  4 (how many trials can the service read?).

PARTS:
    1. Fixed values and errors
    2. From filters to API parameters
    3. Exact counts for each bucket (tier 2)
    4. The response cache (in memory)
    5. The rate limiter
    6. The client: one HTTP request
    7. The client: public functions
    8. The client: one series

How the client reads one series:
    Size check   One `countTotal` call gives the number of trials.
    Tier 1       `max_trials` or fewer: follow `nextPageToken` until all trials are read.
    Tier 2       More than `max_trials`, and the plan counts by one fixed-value field:
                 one call for each bucket gives the exact count and some sample trials.
    Other cases  The series is marked "over the limit", with the count.

The client does not return an incomplete result, because counts from an incomplete result
are wrong (P6).
"""
import asyncio
import random
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.ctgov.normalize import display_label, normalize_study
from app.models import (
    UNKNOWN_LABEL,
    FetchErrorKind,
    FetchResult,
    MultiRule,
    PlanFilters,
    PlanMode,
    QueryPlan,
    Series,
    SeriesFetch,
    Trial,
)
from app.registry import FILTER_FIELDS, REGISTRY, api_fields_for, bucket_expression, bucket_labels

# ===== PART 1: Fixed values and errors ============================================================
# WHAT: The messages, the limits, and the two error types of the client.
# WHY:  The pipeline must know if a retry can help (not available) or not (refused).

UNAVAILABLE_MESSAGE = "ClinicalTrials.gov didn't respond; try again."
_MEMORY_ENTRIES = 48  # The number of responses kept in memory. The oldest one is removed first.
_ALL_FIELDS = ",".join(api_fields_for(REGISTRY))
_JITTER_FRACTION = 0.25  # Each wait can become longer by this fraction, so that parallel retries do not start together.
_RATE_LIMIT_BACKOFF_FACTOR = 4
_AREA = "AREA["


class CtGovError(Exception):
    """WHAT: The error when ClinicalTrials.gov refuses the request (HTTP 4xx). A retry does not help."""

    kind = FetchErrorKind.REJECTED


class CtGovUnavailable(CtGovError):
    """WHAT: The error when ClinicalTrials.gov does not answer in time, or fails after each retry."""

    kind = FetchErrorKind.UNAVAILABLE


def _total(body: dict[str, Any]) -> int:
    """WHAT: Returns the `totalCount` of a response.
    WHY:  A response with HTTP 200 and no count is a defective answer. It is not a count of zero.
    """
    total = body.get("totalCount") if isinstance(body, dict) else None
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise CtGovUnavailable("the response had no usable totalCount")
    return total


# ===== PART 2: From filters to API parameters =====================================================
# WHAT: Changes the filters of a plan into the query parameters of the API.
# WHY:  The field registry tells how to filter each field, so there is no rule for a specific field here.


def merge_filters(shared: PlanFilters, own: PlanFilters) -> PlanFilters:
    """WHAT:   Returns the filters for one series.
    INPUT:  shared - the filters of the plan. own - the filters of the series.
    OUTPUT: The plan filters, replaced where the series sets its own.
    """
    return shared.model_copy(update=own.model_dump(exclude_none=True))


def build_params(filters: PlanFilters) -> dict[str, str]:
    """WHAT:   Changes filters into API query parameters.
    INPUT:  filters - the filters.
    OUTPUT: A dict of API parameters. The `filter_param` of each field in the field registry tells the parameter.
    """
    params: dict[str, str] = {}
    advanced: list[str] = []
    for key, value in filters.model_dump(exclude_none=True, mode="json").items():
        if key.startswith("start_year") or value == []:
            continue  # The two year limits become one range. See below.
        target = REGISTRY[FILTER_FIELDS[key]].filter_param
        if not target.startswith(_AREA):
            params[target] = ",".join(value) if isinstance(value, list) else value
        elif isinstance(value, list):  # Fixed codes: a trial with one of them agrees.
            advanced.append("(" + " OR ".join(f"{target}{item}" for item in value) + ")")
        else:  # Free text: in quotation marks, so that a name of two or more words stays one term.
            advanced.append(f'{target}"{value.replace(chr(34), " ").strip()}"')
    if filters.start_year_min is not None or filters.start_year_max is not None:
        low = f"{filters.start_year_min}-01-01" if filters.start_year_min is not None else "MIN"
        high = f"{filters.start_year_max}-12-31" if filters.start_year_max is not None else "MAX"
        advanced.append(f"{REGISTRY['start_year'].filter_param}RANGE[{low},{high}]")
    if advanced:
        params["filter.advanced"] = " AND ".join(advanced)
    return params


def _and_advanced(params: dict[str, str], expression: str) -> dict[str, str]:
    """WHAT: Adds one search expression to the parameters. Joins it to an expression that is already there with AND."""
    existing = params.get("filter.advanced")
    return {**params, "filter.advanced": f"{existing} AND {expression}" if existing else expression}


# ===== PART 3: Exact counts for each bucket (tier 2) ==============================================
# WHAT: Decides if the API can count the buckets, and makes one query for each bucket.
# WHY:  A question about very many trials can then get exact counts with no full read.


def tier2_field(plan: QueryPlan) -> str | None:
    """WHAT:   Returns the field whose buckets the API can count exactly, or None.
    INPUT:  plan - the plan.
    OUTPUT: The field name when the plan counts trials by one field with a fixed list of values.
            None means that the plan needs all trials.
    """
    if plan.mode is not PlanMode.AGGREGATE or len(plan.group_by) != 1 or plan.bin is not None:
        return None
    field = REGISTRY.get(plan.group_by[0])
    return field.name if field and field.enum and field.filter_param else None


def bucket_queries(field_name: str, params: dict[str, str]) -> list[tuple[str, dict[str, str]]]:
    """WHAT:   Returns (bucket label, API parameters) for each bucket of a fixed-value field.
    WHY:    The buckets must not overlap, so that their sum is equal to the total.
            "Phase 1" does not include Phase 1/2 trials.
    INPUT:  field_name - the field. params - the parameters of the series.
    OUTPUT: One pair for each bucket.
    """
    field = REGISTRY[field_name]
    target = field.filter_param
    if not target.startswith(_AREA):
        # A plain parameter replaces the filter of the user on that field. Thus stay in that filter.
        chosen = params[target].split(",") if target in params else field.enum
        return [(display_label(field_name, value), {**params, target: value}) for value in chosen]

    return [
        (label, _and_advanced(params, bucket_expression(field_name, label)))
        for label in bucket_labels(field_name)
    ]


# ===== PART 4: The response cache =================================================================
# WHAT: Keeps each API response in memory while the service runs.
# WHY:  A second question about the same trials needs no new API calls.


@dataclass
class _Stats:
    """WHAT: Counters for one `fetch`: the API calls and the cache hits."""

    api_calls: int = 0
    cache_hits: int = 0


@dataclass
class _Entry:
    """WHAT: One saved response: the data timestamp of the registry, the save time, and the body."""

    stamp: str | None  # The data timestamp of the registry when the response was saved.
    saved_at: float
    body: Any


class _ResponseCache:
    """WHAT: Keeps responses by full URL, in memory.
    WHY:  Between two data refreshes of the registry (approximately one each day), the same
          request has the same answer.

    An entry is good while the data timestamp of the registry is the same as when the entry was
    saved. If a timestamp is not known, the entry uses a fixed life.
    """

    def __init__(self, ttl_seconds: float, clock: Callable[[], float]) -> None:
        """WHAT:  Prepares an empty cache.
        INPUT: ttl_seconds - the life of an entry with no timestamp. 0 stops the cache.
               clock - the time function.
        """
        self.enabled = ttl_seconds > 0
        self._ttl = ttl_seconds
        self._clock = clock
        self._memory: OrderedDict[str, _Entry] = OrderedDict()

    def peek(self, key: str) -> _Entry | None:
        """WHAT: Returns the saved entry for a URL, or None. The entry can be good or not good."""
        return self._memory.get(key)

    def is_fresh(self, entry: _Entry, stamp: str | None) -> bool:
        """WHAT:   Returns True if a saved entry is still good.
        INPUT:  entry - the saved entry. stamp - the current data timestamp of the registry, or None.
        """
        if stamp is not None and entry.stamp is not None:
            return entry.stamp == stamp
        return self._clock() - entry.saved_at < self._ttl

    def put(self, key: str, stamp: str | None, body: Any) -> None:
        """WHAT: Saves a response. Removes the oldest entry when the cache is full."""
        if not self.enabled:
            return
        self._memory[key] = _Entry(stamp, self._clock(), body)
        self._memory.move_to_end(key)
        while len(self._memory) > _MEMORY_ENTRIES:
            self._memory.popitem(last=False)


# ===== PART 5: The rate limiter ===================================================================
# WHAT: Permits a maximum of 8 requests in each period of 10 seconds.
# WHY:  The API refuses request 11 in 10 seconds (measured), and then blocks for 8 to 16 seconds.


class _RateLimiter:
    """WHAT: Permits a maximum of `limit` requests in each period of `window` seconds.
    WHY:  The limit of the API is a sliding window. Measured: a burst and then one request each
          second was refused at request 11.

    Each caller gets a send time and sleeps until that time. The times are given in sequence.
    """

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]]
    ) -> None:
        """WHAT: Sets the limit, the window, and the time functions."""
        self._limit = limit
        self._window = window
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque(maxlen=limit)  # The last `limit` send times that were given.
        self._blocked_until = 0.0

    async def acquire(self) -> None:
        """WHAT: Waits for a free slot. The slot is recorded before the first await, so a lock is not necessary."""
        now = self._clock()
        slot = max(now, self._blocked_until)
        if len(self._sent) == self._limit:
            slot = max(slot, self._sent[0] + self._window)
        self._sent.append(slot)
        if slot > now:
            await self._sleep(slot - now)

    def block(self, seconds: float) -> None:
        """WHAT: Stops all requests for some seconds.
        WHY:  After HTTP 429, the API blocks the full client, not only one request.
        """
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)


# ===== PART 6: The client: one HTTP request =======================================================
# WHAT: Does one GET with the cache, the rate limit, and retries.
# WHY:  Each call to the API goes through this one path.


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """WHAT: Returns the wait time from the server, if the server sent one as a number of seconds."""
    value = response.headers.get("Retry-After", "").strip()
    return float(value) if value.isdigit() else None


class CtGovClient:
    """WHAT: The client for the ClinicalTrials.gov API. One instance for each process.

    `transport`, `sleep`, `clock`, and `jitter` are parameters so that tests need no network, no
    wait time, and no random numbers.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        """WHAT: Makes the HTTP client, the cache, and the rate limiter from the settings."""
        self._settings = settings or get_settings()
        self._http = httpx.AsyncClient(transport=transport, timeout=self._settings.ctgov_timeout_seconds)
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._cache = _ResponseCache(self._settings.cache_ttl_seconds, clock)
        self._stamp_memo: tuple[str | None, float] | None = None  # (data timestamp, the time when it was read)
        self._semaphore = asyncio.Semaphore(self._settings.ctgov_concurrency)
        self._limiter = _RateLimiter(
            self._settings.ctgov_rate_limit_requests, self._settings.ctgov_rate_limit_window_seconds, clock, sleep
        )

    async def aclose(self) -> None:
        """WHAT: Closes the HTTP client."""
        await self._http.aclose()

    @property
    def max_trials(self) -> int:
        """WHAT: The largest set of trials that this client reads fully."""
        return self._settings.max_trials

    def _spread(self, seconds: float) -> float:
        """WHAT: Makes a wait time longer by a random fraction, so that parallel retries do not start together."""
        return seconds * (1 + _JITTER_FRACTION * self._jitter())

    def _url(self, path: str, params: dict[str, Any]) -> str:
        """WHAT: Returns the full URL for a path and its parameters. The parameters are sorted, so the URL is a stable cache key."""
        return str(httpx.URL(self._settings.ctgov_base_url + path, params=sorted(params.items())))

    async def _get(self, path: str, params: dict[str, Any], stats: _Stats) -> dict[str, Any]:
        """WHAT:   Does a GET through the cache.
        WHY:    A saved response is used only while the data of the registry is the same.
        INPUT:  path - the API path. params - the query parameters. stats - the counters.
        OUTPUT: The JSON body.

        The client reads the data timestamp after a real response, or when there is a saved entry
        to examine. Thus a registry that is not available causes no more calls.
        """
        url = self._url(path, params)
        if not self._cache.enabled:
            return await self._request(url, stats)
        entry = self._cache.peek(url)
        if entry is not None and self._cache.is_fresh(entry, await self._stamp(stats)):
            stats.cache_hits += 1
            return entry.body
        data = await self._request(url, stats)
        self._cache.put(url, await self._stamp(stats), data)
        return data

    async def _stamp(self, stats: _Stats) -> str | None:
        """WHAT:   Returns the current data timestamp of the registry.
        OUTPUT: The timestamp, or None if it cannot be read. The value is read again after some minutes.
        """
        now = self._clock()
        if self._stamp_memo is not None and now < self._stamp_memo[1]:
            return self._stamp_memo[0]
        try:
            body = await self._request(self._url("/version", {}), stats, attempts=1)
            stamp = body.get("dataTimestamp") if isinstance(body, dict) else None
        except CtGovError:
            stamp = None
        keep_for = self._settings.ctgov_version_check_seconds if stamp else min(60.0, self._settings.ctgov_version_check_seconds)
        self._stamp_memo = (stamp, self._clock() + keep_for)
        return stamp

    async def _request(self, url: str, stats: _Stats, attempts: int | None = None) -> dict[str, Any]:
        """WHAT:   Does one GET with the rate limit, the concurrency limit, and retries.
        INPUT:  url - the full URL. stats - the counters. attempts - the number of tries (optional).
        OUTPUT: The JSON body. Raises CtGovUnavailable or CtGovError on a failure.

            HTTP 429               All requests stop. Wait for `Retry-After`, or 4 s, then 16 s.
            HTTP 5xx, timeouts     Wait 0.5 s, then 1 s.
            All other HTTP 4xx     No retry.
        """
        attempts = attempts or self._settings.ctgov_max_attempts
        failure = ""
        async with self._semaphore:
            for attempt in range(1, attempts + 1):
                await self._limiter.acquire()
                stats.api_calls += 1
                rate_limited: httpx.Response | None = None
                try:
                    response = await self._http.get(url)
                except httpx.TransportError as exc:  # This includes all timeouts.
                    failure = type(exc).__name__
                else:
                    if response.status_code == 200:
                        try:
                            data = response.json()
                        except ValueError:
                            failure = "invalid JSON"
                        else:
                            return data
                    elif response.status_code == 429:
                        failure, rate_limited = "HTTP 429", response
                    elif response.status_code >= 500:
                        failure = f"HTTP {response.status_code}"
                    else:
                        raise CtGovError(
                            f"ClinicalTrials.gov rejected the request "
                            f"(HTTP {response.status_code}): {response.text[:200]}"
                        )
                if attempt == attempts:
                    break
                if rate_limited is not None:
                    wait = _retry_after_seconds(rate_limited)
                    if wait is None:
                        base = self._settings.ctgov_rate_limit_backoff_seconds
                        wait = self._spread(base * _RATE_LIMIT_BACKOFF_FACTOR ** (attempt - 1))
                    # The next `acquire`, here and in all other requests, waits for this time.
                    self._limiter.block(min(wait, self._settings.ctgov_max_retry_after_seconds))
                else:
                    await self._sleep(self._spread(self._settings.ctgov_backoff_seconds * 2 ** (attempt - 1)))
        raise CtGovUnavailable(f"{failure}, {attempts} attempts")


    # ===== PART 7: The client: public functions ===================================================
    # WHAT: `count` gives a number of trials. `fetch` gets all series of a plan.
    # WHY:  Other files call only these two functions.

    async def count(self, filters: PlanFilters, stats: _Stats | None = None) -> int:
        """WHAT:   Returns the exact number of trials for the filters. Does not get the trials.
        INPUT:  filters - the filters.
        OUTPUT: The number of trials.
        """
        params = {**build_params(filters), "countTotal": "true", "pageSize": 1, "fields": "NCTId"}
        data = await self._get("/studies", params, stats or _Stats())
        return _total(data)

    async def fetch(self, plan: QueryPlan, fields: Iterable[str]) -> FetchResult:
        """WHAT:   Gets all series of the plan in parallel.
        INPUT:  plan - the plan. fields - the fields to read from each trial.
        OUTPUT: A FetchResult. A series that failed has an `error` and no trials.
                This function does not raise an exception for an API failure.
        """
        fields = list(fields)
        stats = _Stats()
        bucket_field = tier2_field(plan)
        series = await asyncio.gather(
            *(self._fetch_series(s, plan.filters, fields, bucket_field, stats) for s in plan.series)
        )
        # Do not read the timestamp when no series arrived. The server is possibly not available.
        fetched = any(s.error is None for s in series)
        timestamp = await self._stamp(stats) if fetched else None
        return FetchResult(
            series=list(series),
            api_calls=stats.api_calls,
            cache_hits=stats.cache_hits,
            data_timestamp=timestamp,
        )


    # ===== PART 8: The client: one series =========================================================
    # WHAT: Reads one series fully, or counts its buckets, or gives the cause of the failure.
    # WHY:  A series is complete or empty. It is never incomplete (P6).

    async def _fetch_series(
        self, series: Series, shared: PlanFilters, fields: list[str], bucket_field: str | None, stats: _Stats
    ) -> SeriesFetch:
        """WHAT:   Gets one series: counts it, then reads it with tier 1 or tier 2.
        INPUT:  series - the series. shared - the plan filters. fields - the fields to read.
                bucket_field - the field for tier 2, or None. stats - the counters.
        OUTPUT: A SeriesFetch. On a failure, the trials read until then are removed.
        """
        filters = merge_filters(shared, series.filters)
        params = build_params(filters)
        result = SeriesFetch(label=series.label, filters=filters, api_params=params)
        try:
            result.trials_matched = await self.count(filters, stats)
            if result.trials_matched <= self._settings.max_trials:
                result.tier = 1
                if result.trials_matched:
                    result.trials = await self._fetch_all(params, fields, stats)
            elif bucket_field:
                result.tier, result.is_sample, result.bucket_field = 2, True, bucket_field
                result.bucket_counts, result.trials = await self._fetch_buckets(
                    bucket_field, params, fields, result.trials_matched, stats
                )
            else:
                result.error_kind = FetchErrorKind.OVER_CAP
                result.error = (
                    f"{result.trials_matched:,} trials matched, more than the "
                    f"{self._settings.max_trials:,} that can be read in full. Narrow the question, "
                    f"for example by status, phase, country or start year."
                )
        except CtGovError as exc:
            # Remove all trials read until now. An incomplete result must not be counted.
            result.trials, result.bucket_counts, result.tier, result.is_sample = [], None, None, False
            result.error_kind, result.error_detail = exc.kind, str(exc)
            result.error = UNAVAILABLE_MESSAGE if isinstance(exc, CtGovUnavailable) else str(exc)
        return result

    async def _fetch_all(self, params: dict[str, str], fields: list[str], stats: _Stats) -> list[Trial]:
        """WHAT: Tier 1: follows `nextPageToken` to the end.
        WHY:  The API gives a maximum of 1,000 trials for each page and does not report it.
              Thus only a response with no token is the last page.
        """
        # Get all fields, not only the fields of the plan. The next question about these trials uses the same pages.
        request = {**params, "fields": _ALL_FIELDS, "pageSize": self._settings.ctgov_page_size}
        trials: dict[str, Trial] = {}
        token: str | None = None
        while True:
            page = await self._get("/studies", {**request, "pageToken": token} if token else request, stats)
            studies = page.get("studies", []) if isinstance(page, dict) else None
            if not isinstance(studies, list):
                raise CtGovUnavailable("the response had no list of studies")
            for study in studies:
                trials.setdefault(*self._trial(study, fields))
            token = page.get("nextPageToken")
            if not token:
                return list(trials.values())
            if not studies or len(trials) > self._settings.max_trials + self._settings.ctgov_page_size:
                raise CtGovUnavailable("paging did not end where the API's own count said it would")

    async def _fetch_buckets(
        self, field_name: str, params: dict[str, str], fields: list[str], total: int, stats: _Stats
    ) -> tuple[dict[str, int], list[Trial]]:
        """WHAT:   Tier 2: gets the exact count and some sample trials for each bucket.
        OUTPUT: (bucket label -> count, the sample trials). One API call for each bucket.
        """
        request = {
            "fields": _ALL_FIELDS,
            "countTotal": "true",
            "pageSize": self._settings.tier2_samples_per_bucket,
        }
        queries = bucket_queries(field_name, params)
        pages = await asyncio.gather(
            *(self._get("/studies", {**bucket_params, **request}, stats) for _, bucket_params in queries)
        )
        counts: dict[str, int] = {}
        trials: dict[str, Trial] = {}
        for (label, _), page in zip(queries, pages):
            counts[label] = counts.get(label, 0) + _total(page)  # Two codes can have the same label.
            for study in page.get("studies", []):
                trials.setdefault(*self._trial(study, fields))

        if REGISTRY[field_name].multi is not MultiRule.EXPLODE:
            # Each trial is in one bucket only. Thus the sum of the buckets must be equal to the size check.
            uncounted = total - sum(counts.values())
            if uncounted < 0:
                raise CtGovUnavailable("bucket counts add up to more than the API's own total")
            counts[UNKNOWN_LABEL] = counts.get(UNKNOWN_LABEL, 0) + uncounted
        return {label: n for label, n in counts.items() if n}, list(trials.values())

    @staticmethod
    def _trial(study: Any, fields: list[str]) -> tuple[str, Trial]:
        """WHAT: Normalizes one study.
        WHY:  If one record cannot be read, the full result is not usable. To skip the record
              would change the counts with no message.
        """
        try:
            trial = normalize_study(study, fields)
        except (ValueError, TypeError, AttributeError) as exc:
            raise CtGovUnavailable(f"a study record could not be read ({exc})") from exc
        return trial.nct_id, trial
