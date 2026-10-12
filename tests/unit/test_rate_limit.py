"""TASK: Tests the rate limiter and the wait times after HTTP 429.
GRADING: System Design (real API limits).

Time is fake (`Sleeper`), so every wait is asserted exactly and nothing really sleeps.
"""
import httpx
import pytest

from app.config import Settings
from app.ctgov.client import CtGovClient, _RateLimiter
from app.models import FetchErrorKind, PlanFilters, QueryPlan
from tests.fakes import FakeCtGov, Sleeper, clone_studies, make_client

LIMIT, WINDOW = 8, 10.0


@pytest.fixture
def studies(load_fixture):
    return clone_studies(load_fixture("placebo_arms")["studies"][0], 5)


def timed_server(sleeper: Sleeper, studies=None, override=None):
    """Returns a fake API that records the (fake) time when each request arrived."""
    arrivals: list[float] = []

    def record(request: httpx.Request) -> httpx.Response | None:
        arrivals.append(sleeper.now)
        return override(request) if override else None

    return FakeCtGov(studies or [], override=record), arrivals


def most_in_any_window(arrivals: list[float], window: float) -> int:
    return max(sum(1 for t in arrivals if start <= t < start + window) for start in arrivals)


# ---- the limiter itself -------------------------------------------------------------------------


async def test_limiter_lets_a_full_window_through_without_waiting():
    sleeper = Sleeper()
    limiter = _RateLimiter(LIMIT, WINDOW, sleeper.clock, sleeper)
    for _ in range(LIMIT):
        await limiter.acquire()
    assert sleeper.delays == []


async def test_limiter_makes_the_next_request_wait_for_the_window_to_slide():
    sleeper = Sleeper()
    limiter = _RateLimiter(LIMIT, WINDOW, sleeper.clock, sleeper)
    for _ in range(LIMIT + 1):
        await limiter.acquire()
    assert sleeper.delays == [10.0]


async def test_limiter_never_allows_more_than_the_limit_in_any_window():
    sleeper = Sleeper()
    limiter = _RateLimiter(LIMIT, WINDOW, sleeper.clock, sleeper)
    sent = []
    for i in range(50):
        await limiter.acquire()
        sent.append(sleeper.now)
        sleeper.now += 0.3 * (i % 4)  # uneven gaps between callers
    assert most_in_any_window(sent, WINDOW) == LIMIT


async def test_limiter_frees_slots_as_time_passes():
    sleeper = Sleeper()
    limiter = _RateLimiter(LIMIT, WINDOW, sleeper.clock, sleeper)
    for _ in range(LIMIT):
        await limiter.acquire()
    sleeper.now += WINDOW
    for _ in range(LIMIT):
        await limiter.acquire()
    assert sleeper.delays == []


async def test_limiter_block_holds_every_later_request():
    sleeper = Sleeper()
    limiter = _RateLimiter(LIMIT, WINDOW, sleeper.clock, sleeper)
    limiter.block(4.0)
    await limiter.acquire()
    await limiter.acquire()  # after the block has passed: no further wait
    assert sleeper.delays == [4.0]


async def test_limiter_block_is_never_shortened_by_a_later_shorter_one():
    sleeper = Sleeper()
    limiter = _RateLimiter(LIMIT, WINDOW, sleeper.clock, sleeper)
    limiter.block(16.0)
    limiter.block(4.0)
    await limiter.acquire()
    assert sleeper.delays == [16.0]


# ---- through the client -------------------------------------------------------------------------


async def test_client_never_sends_more_than_eight_requests_in_ten_seconds(studies):
    sleeper = Sleeper()
    server, arrivals = timed_server(sleeper, studies)
    client = make_client(server, sleeper, cache_ttl_seconds=0)
    for i in range(30):
        await client.count(PlanFilters(condition=f"condition {i}"))
    assert len(arrivals) == 30
    assert most_in_any_window(arrivals, WINDOW) == LIMIT
    assert sleeper.delays == [10.0, 10.0, 10.0]  # one wait per full window


async def test_tier2_burst_is_spread_over_two_windows(load_fixture, studies):
    """Lung cancer by status needs 16 calls. The real API refuses call 11 in 10 s."""

    def serve(request: httpx.Request) -> httpx.Response | None:
        params = request.url.params
        if request.url.path.endswith("/version"):
            return None
        if params.get("pageSize") == "1":
            return httpx.Response(200, json=load_fixture("count_lung"))
        total = 14613 if params.get("filter.overallStatus") == "COMPLETED" else 0
        return httpx.Response(200, json={"totalCount": total, "studies": studies[:1] if total else []})

    sleeper = Sleeper()
    server, arrivals = timed_server(sleeper, override=serve)
    plan = QueryPlan(mode="aggregate", filters={"condition": "lung cancer"}, group_by=["status"])
    result = await make_client(server, sleeper).fetch(plan, ["status"])

    [series] = result.series
    assert series.tier == 2 and series.bucket_counts == {"Completed": 14613}
    assert len(arrivals) == 16  # size check + 14 statuses + version
    assert most_in_any_window(arrivals, WINDOW) == LIMIT
    assert sleeper.now - 1000.0 == 10.0  # total wait: one window


async def test_cached_responses_do_not_use_up_the_rate_limit(studies):
    sleeper = Sleeper()
    server, arrivals = timed_server(sleeper, studies)
    client = make_client(server, sleeper)
    for _ in range(30):
        await client.count(PlanFilters(condition="asthma"))
    assert len(arrivals) == 2 and sleeper.delays == []  # the search once, the data timestamp once


async def test_rate_limit_is_configurable(studies):
    sleeper = Sleeper()
    server, arrivals = timed_server(sleeper, studies)
    client = make_client(
        server, sleeper, cache_ttl_seconds=0, ctgov_rate_limit_requests=2, ctgov_rate_limit_window_seconds=1.0
    )
    for i in range(6):
        await client.count(PlanFilters(condition=f"condition {i}"))
    assert most_in_any_window(arrivals, 1.0) == 2
    assert sleeper.delays == [1.0, 1.0]


# ---- 429 ----------------------------------------------------------------------------------------


def refuse_first(n: int, **headers: str):
    seen = 0

    def override(request: httpx.Request) -> httpx.Response | None:
        nonlocal seen
        seen += 1
        return httpx.Response(429, headers=headers, text="<!doctype html><title>429</title>") if seen <= n else None

    return override


async def test_429_waits_long_enough_to_outlast_the_block(studies):
    """The measured block is 8 to 16 s. Waits of 4 s then 16 s are sufficient. Waits of 0.5 s then 1 s were not."""
    sleeper = Sleeper()
    server, arrivals = timed_server(sleeper, studies, refuse_first(2))
    assert await make_client(server, sleeper).count(PlanFilters(condition="asthma")) == 5
    assert sleeper.delays == [4.0, 16.0]
    assert [t - 1000.0 for t in arrivals[:3]] == [0.0, 4.0, 20.0]  # the fourth is the timestamp read


async def test_429_uses_the_servers_retry_after_when_it_sends_one(studies):
    sleeper = Sleeper()
    server, _ = timed_server(sleeper, studies, refuse_first(1, **{"Retry-After": "7"}))
    await make_client(server, sleeper).count(PlanFilters(condition="asthma"))
    assert sleeper.delays == [7.0]


async def test_an_absurd_retry_after_is_capped(studies):
    sleeper = Sleeper()
    server, _ = timed_server(sleeper, studies, refuse_first(1, **{"Retry-After": "86400"}))
    await make_client(server, sleeper).count(PlanFilters(condition="asthma"))
    assert sleeper.delays == [60.0]


async def test_a_retry_after_that_is_not_a_number_falls_back_to_backoff(studies):
    sleeper = Sleeper()
    server, _ = timed_server(sleeper, studies, refuse_first(1, **{"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    await make_client(server, sleeper).count(PlanFilters(condition="asthma"))
    assert sleeper.delays == [4.0]


async def test_429_on_every_attempt_gives_up_and_says_why_in_the_detail(studies):
    sleeper = Sleeper()
    server, arrivals = timed_server(sleeper, studies, refuse_first(99))
    plan = QueryPlan(mode="aggregate", group_by=["country"])
    [series] = (await make_client(server, sleeper).fetch(plan, ["country"])).series
    assert series.error_kind is FetchErrorKind.UNAVAILABLE
    assert series.error_detail == "HTTP 429, 3 attempts"
    assert len(arrivals) == 3 and sleeper.delays == [4.0, 16.0]


async def test_a_429_holds_back_requests_that_come_after_it(studies):
    """The block applies to the client, not only to the request that was refused."""
    sleeper = Sleeper()
    server, arrivals = timed_server(sleeper, studies, lambda r: httpx.Response(429))
    client = make_client(server, sleeper, ctgov_max_attempts=1, cache_ttl_seconds=0)
    plan = QueryPlan(mode="aggregate", group_by=["country"])
    await client.fetch(plan, ["country"])  # refused once; with one attempt there is no retry...
    assert sleeper.delays == []
    client._limiter.block(4.0)  # ...so apply the block a retry would have applied
    await client.fetch(plan, ["country"])
    assert [t - 1000.0 for t in arrivals] == [0.0, 4.0]


# ---- jitter -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "jitter", "expected"),
    [
        (500, 0.0, [0.5, 1.0]),
        (500, 1.0, [0.625, 1.25]),  # up to 25 % longer
        (429, 0.0, [4.0, 16.0]),
        (429, 1.0, [5.0, 20.0]),
    ],
)
async def test_waits_are_stretched_by_jitter_so_parallel_retries_spread_out(studies, status, jitter, expected):
    sleeper = Sleeper()
    seen = 0

    def fail_twice(request: httpx.Request) -> httpx.Response | None:
        nonlocal seen
        seen += 1
        return httpx.Response(status) if seen <= 2 else None

    client = CtGovClient(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(FakeCtGov(studies, override=fail_twice)),
        sleep=sleeper,
        clock=sleeper.clock,
        jitter=lambda: jitter,
    )
    assert await client.count(PlanFilters(condition="asthma")) == 5
    assert sleeper.delays == expected
