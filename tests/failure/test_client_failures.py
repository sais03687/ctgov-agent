"""TASK: Tests the API client when the API fails.
GRADING: Code Quality (robustness).
"""
import httpx
import pytest

from app.ctgov.client import UNAVAILABLE_MESSAGE
from app.models import FetchErrorKind, PlanFilters, QueryPlan
from tests.fakes import FakeCtGov, Sleeper, clone_studies, make_client

PLAN = QueryPlan(mode="aggregate", filters={"condition": "lung cancer"}, group_by=["country"])


@pytest.fixture
def studies(load_fixture):
    return clone_studies(load_fixture("placebo_arms")["studies"][0], 25)


def failing_first(n: int, make_failure):
    """Returns an override that fails the first `n` requests. After that, the fake server answers."""
    seen = 0

    def override(request: httpx.Request) -> httpx.Response | None:
        nonlocal seen
        seen += 1
        return make_failure(request) if seen <= n else None

    return override


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


# ---- retries ------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503])  # 429 has its own rules: tests/unit/test_rate_limit.py
async def test_server_error_is_retried_with_exponential_backoff(studies, status):
    server = FakeCtGov(studies, override=failing_first(2, lambda r: httpx.Response(status)))
    sleeper = Sleeper()
    result = await make_client(server, sleeper).fetch(PLAN, ["country"])
    assert result.series[0].error is None
    assert len(result.series[0].trials) == 25
    assert sleeper.delays == [0.5, 1.0]
    assert result.api_calls == 5  # 2 failures + size check + page + version


async def test_timeout_is_retried_then_succeeds(studies):
    server = FakeCtGov(studies, override=failing_first(1, timeout))
    sleeper = Sleeper()
    result = await make_client(server, sleeper).fetch(PLAN, ["country"])
    assert len(result.series[0].trials) == 25
    assert sleeper.delays == [0.5]


async def test_timeouts_on_every_attempt_give_up_after_three_tries(studies):
    server = FakeCtGov(studies, override=timeout)
    sleeper = Sleeper()
    result = await make_client(server, sleeper).fetch(PLAN, ["country"])
    [series] = result.series
    assert series.error == UNAVAILABLE_MESSAGE
    assert series.error_kind is FetchErrorKind.UNAVAILABLE
    assert series.error_detail == "ReadTimeout, 3 attempts"  # kept for the trace, not shown to users
    assert series.trials == [] and series.tier is None
    assert len(server.requests) == 3  # 3 tries of the size check, and no version call after
    assert sleeper.delays == [0.5, 1.0]
    assert result.data_timestamp is None


async def test_persistent_500_gives_up_after_three_tries(studies):
    server = FakeCtGov(studies, override=lambda r: httpx.Response(500))
    result = await make_client(server).fetch(PLAN, ["country"])
    assert result.series[0].error_kind is FetchErrorKind.UNAVAILABLE
    assert result.series[0].error_detail == "HTTP 500, 3 attempts"
    assert len(server.requests) == 3


async def test_connection_error_counts_as_unavailable(studies):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await make_client(FakeCtGov(studies, override=refuse)).fetch(PLAN, ["country"])
    assert result.series[0].error == UNAVAILABLE_MESSAGE


async def test_a_body_that_is_not_json_is_retried(studies):
    server = FakeCtGov(studies, override=failing_first(1, lambda r: httpx.Response(200, text="<html>oops")))
    sleeper = Sleeper()
    result = await make_client(server, sleeper).fetch(PLAN, ["country"])
    assert result.series[0].error is None and sleeper.delays == [0.5]


async def test_attempt_count_is_configurable(studies):
    server = FakeCtGov(studies, override=lambda r: httpx.Response(503))
    await make_client(server, ctgov_max_attempts=2).fetch(PLAN, ["country"])
    assert len(server.requests) == 2


# ---- requests the API refuses -------------------------------------------------------------------


async def test_a_400_is_not_retried_and_its_reason_is_kept(studies):
    refusal = httpx.Response(400, text="Error parsing query in advanced filter: Unknown area name: `Nope`")
    server = FakeCtGov(studies, override=lambda r: refusal)
    sleeper = Sleeper()
    result = await make_client(server, sleeper).fetch(PLAN, ["country"])
    [series] = result.series
    assert series.error_kind is FetchErrorKind.REJECTED
    assert "HTTP 400" in series.error and "Unknown area name" in series.error
    assert len(server.requests) == 1 and sleeper.delays == []


async def test_count_raises_so_the_planner_tool_can_report_the_failure():
    from app.ctgov.client import CtGovUnavailable

    client = make_client(FakeCtGov(override=lambda r: httpx.Response(500)))
    with pytest.raises(CtGovUnavailable):
        await client.count(PlanFilters(condition="lung cancer"))


# ---- nothing partial ----------------------------------------------------------------------------


async def test_failure_in_the_middle_of_paging_returns_no_trials_at_all(studies):
    """Pages 1 and 2 arrive, and page 3 does not. The 20 trials already read must not be used."""

    def break_third_page(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(500) if request.url.params.get("pageToken") == "20" else None

    server = FakeCtGov(studies, page_cap=10, override=break_third_page)
    result = await make_client(server).fetch(PLAN, ["country"])
    [series] = result.series
    assert series.trials_matched == 25
    assert series.trials == []
    assert series.error == UNAVAILABLE_MESSAGE


async def test_a_token_with_an_empty_page_is_treated_as_a_failure(studies):
    def empty_page_with_token(request: httpx.Request) -> httpx.Response | None:
        if request.url.params.get("pageToken") == "10":
            return httpx.Response(200, json={"studies": [], "nextPageToken": "10"})
        return None

    server = FakeCtGov(studies, page_cap=10, override=empty_page_with_token)
    [series] = (await make_client(server).fetch(PLAN, ["country"])).series
    assert series.error_kind is FetchErrorKind.UNAVAILABLE and series.trials == []


# ---- one series fails, the others survive -------------------------------------------------------


async def test_one_series_failing_does_not_lose_the_others(studies):
    def fail_drug_b(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(500) if request.url.params.get("query.intr") == "B" else None

    plan = QueryPlan(
        mode="aggregate",
        group_by=["country"],
        series=[{"label": name, "filters": {"drug": name}} for name in "ABC"],
    )
    result = await make_client(FakeCtGov(studies, override=fail_drug_b)).fetch(plan, ["country"])
    by_label = {s.label: s for s in result.series}
    assert list(by_label) == ["A", "B", "C"]
    assert by_label["B"].error == UNAVAILABLE_MESSAGE and by_label["B"].trials == []
    assert len(by_label["A"].trials) == len(by_label["C"].trials) == 25
    assert result.data_timestamp is not None


# ---- zero results and a missing timestamp -------------------------------------------------------


async def test_zero_results_is_a_success_with_no_trials():
    result = await make_client(FakeCtGov([])).fetch(PLAN, ["country"])
    [series] = result.series
    assert (series.error, series.tier, series.trials_matched, series.trials) == (None, 1, 0, [])


async def test_version_endpoint_failing_does_not_fail_the_fetch(studies):
    def no_version(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(500) if request.url.path.endswith("/version") else None

    result = await make_client(FakeCtGov(studies, override=no_version)).fetch(PLAN, ["country"])
    assert result.data_timestamp is None
    assert len(result.series[0].trials) == 25
