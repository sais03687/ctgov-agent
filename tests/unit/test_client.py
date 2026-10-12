"""TASK: Tests the API client when the API works: pages, tiers, cache.
GRADING: System Design (real API data).
"""
import asyncio

import httpx
import pytest

from app.ctgov.client import bucket_queries, build_params, merge_filters, tier2_field
from app.models import FetchErrorKind, PlanFilters, QueryPlan
from tests.fakes import VERSION, FakeCtGov, clone_studies, make_client

PHASE_PLAN = QueryPlan(mode="aggregate", filters={"condition": "lung cancer"}, group_by=["phase"])
PHASE_BUCKETS = [
    "Early Phase 1", "Phase 1", "Phase 2", "Phase 3", "Phase 4", "Not Applicable",
    "Phase 1/2", "Phase 2/3", "Unknown",
]


@pytest.fixture
def template(load_fixture):
    return load_fixture("placebo_arms")["studies"][0]


# ---- filters -> parameters ----------------------------------------------------------------------


def test_no_filters_means_no_parameters():
    assert build_params(PlanFilters()) == {}


def test_each_text_filter_goes_to_its_own_query_parameter():
    filters = PlanFilters(condition="lung cancer", drug="pembrolizumab", sponsor="Pfizer", country="Germany")
    assert build_params(filters) == {
        "query.cond": "lung cancer",
        "query.intr": "pembrolizumab",
        "query.spons": "Pfizer",
        "filter.advanced": 'AREA[LocationCountry]"Germany"',
    }


def test_country_is_matched_on_the_country_field_not_on_any_location_text():
    """`query.locn` also searches cities and states. "Georgia" would give the US state."""
    params = build_params(PlanFilters(country="Georgia"))
    assert "query.locn" not in params
    assert params == {"filter.advanced": 'AREA[LocationCountry]"Georgia"'}


def test_country_of_several_words_stays_one_quoted_term():
    assert build_params(PlanFilters(country="United States")) == {
        "filter.advanced": 'AREA[LocationCountry]"United States"'
    }


def test_quotes_in_a_country_name_cannot_break_the_filter_expression():
    params = build_params(PlanFilters(country='Germany" OR AREA[Phase]"PHASE1'))
    assert params["filter.advanced"].count('"') == 2


def test_country_combines_with_phase_and_year_filters():
    filters = PlanFilters(country="Japan", phase=["PHASE3"], start_year_min=2020)
    assert build_params(filters)["filter.advanced"] == (
        'AREA[LocationCountry]"Japan" AND (AREA[Phase]PHASE3) AND AREA[StartDate]RANGE[2020-01-01,MAX]'
    )


def test_status_list_is_comma_joined():
    assert build_params(PlanFilters(status=["RECRUITING", "COMPLETED"])) == {
        "filter.overallStatus": "RECRUITING,COMPLETED"
    }


def test_phase_and_year_filters_are_combined_in_the_advanced_filter():
    filters = PlanFilters(phase=["PHASE2", "PHASE3"], start_year_min=2015)
    assert build_params(filters) == {
        "filter.advanced": "(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) AND AREA[StartDate]RANGE[2015-01-01,MAX]"
    }


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (2015, None, "AREA[StartDate]RANGE[2015-01-01,MAX]"),
        (None, 2020, "AREA[StartDate]RANGE[MIN,2020-12-31]"),
        (2015, 2020, "AREA[StartDate]RANGE[2015-01-01,2020-12-31]"),
    ],
)
def test_year_bounds_become_one_date_range(low, high, expected):
    assert build_params(PlanFilters(start_year_min=low, start_year_max=high)) == {"filter.advanced": expected}


def test_series_filters_replace_plan_filters_only_where_set():
    merged = merge_filters(
        PlanFilters(condition="melanoma", status=["RECRUITING"], drug="nivolumab"),
        PlanFilters(drug="pembrolizumab"),
    )
    assert merged == PlanFilters(condition="melanoma", status=["RECRUITING"], drug="pembrolizumab")


# ---- tier 2 eligibility and bucket queries ------------------------------------------------------


@pytest.mark.parametrize("field", ["phase", "status", "sponsor_class", "intervention_type"])
def test_counting_by_one_fixed_value_field_is_tier2_eligible(field):
    assert tier2_field(QueryPlan(mode="aggregate", group_by=[field])) == field


@pytest.mark.parametrize(
    "plan",
    [
        QueryPlan(mode="aggregate", group_by=["country"]),  # values are not a fixed list
        QueryPlan(mode="aggregate", group_by=["start_year"]),
        QueryPlan(mode="aggregate", group_by=["phase", "status"]),  # two dimensions
        QueryPlan(mode="histogram", bin={"field": "enrollment", "bin_width": 50}),
        QueryPlan(mode="points", x_field="enrollment", y_field="duration_months"),
        QueryPlan(mode="network", network={"source_field": "sponsor", "target_field": "intervention"}),
        QueryPlan(mode="aggregate", group_by=["no_such_field"]),
    ],
)
def test_plans_that_need_every_trial_are_not_tier2_eligible(plan):
    assert tier2_field(plan) is None


def test_phase_buckets_are_disjoint_and_include_combined_and_missing():
    queries = dict(bucket_queries("phase", {"query.cond": "lung cancer"}))
    assert list(queries) == PHASE_BUCKETS
    assert queries["Phase 1"]["filter.advanced"] == (
        "AREA[Phase]PHASE1 AND NOT (AREA[Phase]EARLY_PHASE1 OR AREA[Phase]PHASE2 OR "
        "AREA[Phase]PHASE3 OR AREA[Phase]PHASE4 OR AREA[Phase]NA)"
    )
    assert queries["Phase 1/2"]["filter.advanced"] == "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2"
    assert queries["Unknown"]["filter.advanced"] == "AREA[Phase]MISSING"
    assert all(q["query.cond"] == "lung cancer" for q in queries.values())


def test_bucket_expression_is_added_to_the_users_own_advanced_filter():
    params = build_params(PlanFilters(start_year_min=2015))
    queries = dict(bucket_queries("sponsor_class", params))
    assert queries["Industry"]["filter.advanced"] == (
        "AREA[StartDate]RANGE[2015-01-01,MAX] AND AREA[LeadSponsorClass]INDUSTRY"
    )
    assert "Unknown" in queries


def test_status_buckets_stay_inside_the_users_status_filter():
    params = build_params(PlanFilters(status=["RECRUITING", "COMPLETED"]))
    assert bucket_queries("status", params) == [
        ("Recruiting", {"filter.overallStatus": "RECRUITING"}),  # label for people, code for the API
        ("Completed", {"filter.overallStatus": "COMPLETED"}),
    ]
    assert len(bucket_queries("status", {})) == 14


# ---- paging -------------------------------------------------------------------------------------


async def test_page_size_cap_is_handled_by_following_the_token(template):
    """The real API gives 1000 rows for all larger requests. 2,500 trials need 3 pages."""
    server = FakeCtGov(clone_studies(template, 2500), page_cap=1000)
    client = make_client(server)
    result = await client.fetch(QueryPlan(mode="aggregate", group_by=["country"]), ["country"])

    [series] = result.series
    assert series.tier == 1 and series.error is None
    assert series.trials_matched == 2500
    assert len({t.nct_id for t in series.trials}) == 2500
    pages = server.page_requests()
    assert [p.url.params.get("pageToken") for p in pages] == [None, "1000", "2000"]
    assert all(p.url.params["pageSize"] == "1000" for p in pages)
    assert result.api_calls == 5  # size check + 3 pages + version


async def test_paging_does_not_assume_the_page_size_it_asked_for(template):
    """The server gives 400 rows for each page although the request was 1000. The client still gets all trials in 7 pages."""
    server = FakeCtGov(clone_studies(template, 2500), page_cap=400)
    client = make_client(server)
    result = await client.fetch(QueryPlan(mode="aggregate", group_by=["country"]), ["country"])
    assert len(result.series[0].trials) == 2500
    assert len(server.page_requests()) == 7


async def test_real_first_page_carries_a_token_that_is_followed(load_fixture):
    """`pembrolizumab_page.json` is a real first page with a real `nextPageToken`."""
    first, last = load_fixture("pembrolizumab_page"), load_fixture("phase12")
    token = first["nextPageToken"]
    last = {"studies": last["studies"]}  # a final page has no token

    def serve(request: httpx.Request) -> httpx.Response | None:
        params = request.url.params
        if request.url.path.endswith("/version"):
            return None
        if params.get("pageSize") == "1":
            return httpx.Response(200, json=load_fixture("count_pembro"))
        return httpx.Response(200, json=last if params.get("pageToken") == token else first)

    server = FakeCtGov(override=serve)
    result = await make_client(server).fetch(
        QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab"}, group_by=["phase"]), ["phase"]
    )
    [series] = result.series
    assert series.trials_matched == 2974 and series.tier == 1
    expected = {s["protocolSection"]["identificationModule"]["nctId"] for s in first["studies"] + last["studies"]}
    assert {t.nct_id for t in series.trials} == expected
    assert [r.url.params.get("pageToken") for r in server.page_requests()] == [None, token]


async def test_a_trial_returned_on_two_pages_is_kept_once(template):
    studies = clone_studies(template, 30)
    server = FakeCtGov(studies + studies[:5], page_cap=10)
    result = await make_client(server).fetch(QueryPlan(mode="aggregate", group_by=["phase"]), ["phase"])
    assert len(result.series[0].trials) == 30


async def test_every_field_is_fetched_but_only_the_planned_ones_are_filled_in(template):
    """The client asks for all fields, so that the pages can also answer the next question.

    The trials made for this question still hold only the fields that it asked for.
    """
    server = FakeCtGov(clone_studies(template, 3))
    result = await make_client(server).fetch(PHASE_PLAN, ["phase", "enrollment"])
    [page] = server.page_requests()
    requested = page.url.params["fields"].split(",")
    assert requested[0] == "NCTId" and {"Phase", "LocationCountry", "InterventionName", "ArmGroupType", "BriefTitle"} <= set(requested)
    assert page.url.params["query.cond"] == "lung cancer"
    trial = result.series[0].trials[0]
    assert trial.phase == "Phase 3" and trial.enrollment is not None and trial.country == []
    assert result.data_timestamp == VERSION["dataTimestamp"]


async def test_a_different_question_about_the_same_trials_needs_no_new_pages(template):
    """A phase chart first, then a country chart and a drug network for the same filters.

    The trial pages come from the cache, and each question still gets its own fields.
    """
    server = FakeCtGov(clone_studies(template, 2500))
    client = make_client(server)
    by_phase = await client.fetch(PHASE_PLAN, ["phase"])
    pages_read = len(server.page_requests())
    by_country = await client.fetch(QueryPlan(mode="aggregate", filters={"condition": "lung cancer"}, group_by=["country"]), ["country"])
    network = await client.fetch(
        QueryPlan(mode="network", filters={"condition": "lung cancer"}, network={"source_field": "sponsor", "target_field": "intervention"}),
        ["sponsor", "intervention"],
    )
    assert (by_phase.api_calls, pages_read) == (5, 3)
    assert len(server.page_requests()) == pages_read  # nothing new was fetched
    assert (by_country.api_calls, by_country.cache_hits) == (0, 4) and (network.api_calls, network.cache_hits) == (0, 4)
    assert by_country.series[0].trials[0].country and not by_country.series[0].trials[0].intervention
    assert network.series[0].trials[0].intervention == ["Pembrolizumab", "Gemcitabine", "Cisplatin", "Placebo"]


# ---- tier selection (size-check fixtures) -------------------------------------------------------


def _count_server(load_fixture, count_fixture, template, bucket_totals=None):
    """Answers the size check with a saved count response, and bucket calls with `bucket_totals`."""

    def serve(request: httpx.Request) -> httpx.Response | None:
        params = request.url.params
        if request.url.path.endswith("/version"):
            return None
        if params.get("pageSize") == "1":
            return httpx.Response(200, json=load_fixture(count_fixture))
        if bucket_totals is not None and params.get("countTotal") == "true":
            total = bucket_totals.get(params.get("filter.advanced"), 0)
            return httpx.Response(200, json={"totalCount": total, "studies": [template] if total else []})
        return None

    return FakeCtGov(clone_studies(template, 4), override=serve)


# Live counts for lung cancer trials by phase, taken on 2026-10-11 (they add up to 14,613).
LUNG_PHASE_COUNTS = {
    "Early Phase 1": 136, "Phase 1": 1985, "Phase 2": 4004, "Phase 3": 1232, "Phase 4": 246,
    "Not Applicable": 2436, "Phase 1/2": 1190, "Phase 2/3": 172, "Unknown": 3212,
}


def _lung_bucket_totals(counts=LUNG_PHASE_COUNTS):
    queries = dict(bucket_queries("phase", {"query.cond": "lung cancer"}))
    return {queries[label]["filter.advanced"]: n for label, n in counts.items()}


async def test_under_the_cap_is_tier1(load_fixture, template):
    server = _count_server(load_fixture, "count_pembro", template)
    result = await make_client(server).fetch(
        QueryPlan(mode="aggregate", filters={"drug": "pembrolizumab"}, group_by=["country"]), ["country"]
    )
    [series] = result.series
    assert (series.tier, series.trials_matched, series.is_sample) == (1, 2974, False)
    assert series.bucket_counts is None


async def test_over_the_cap_counting_by_phase_is_tier2_with_exact_bucket_counts(load_fixture, template):
    server = _count_server(load_fixture, "count_lung", template, _lung_bucket_totals())
    result = await make_client(server).fetch(PHASE_PLAN, ["phase"])

    [series] = result.series
    assert (series.tier, series.trials_matched, series.is_sample) == (2, 14613, True)
    assert series.bucket_field == "phase"
    assert series.bucket_counts == LUNG_PHASE_COUNTS
    assert sum(series.bucket_counts.values()) == series.trials_matched
    assert [t.nct_id for t in series.trials] == ["NCT04003636"]  # the sample, de-duplicated
    bucket_calls = [r for r in server.page_requests() if r.url.params.get("countTotal") == "true"]
    assert len(bucket_calls) == len(PHASE_BUCKETS)
    assert all(r.url.params["pageSize"] == "10" for r in bucket_calls)
    assert all("pageToken" not in r.url.params for r in server.study_requests())  # no paging at all


async def test_over_the_cap_and_not_countable_is_refused_with_the_matched_count(load_fixture, template):
    server = _count_server(load_fixture, "count_lung", template)
    result = await make_client(server).fetch(
        QueryPlan(mode="aggregate", filters={"condition": "lung cancer"}, group_by=["country"]), ["country"]
    )
    [series] = result.series
    assert series.error_kind is FetchErrorKind.OVER_CAP
    assert "14,613 trials matched" in series.error and "Narrow the question" in series.error
    assert series.trials == [] and series.tier is None
    assert server.page_requests() == []  # not one page was fetched
    assert result.data_timestamp is None


async def test_the_cap_is_inclusive_and_configurable(template):
    plan = QueryPlan(mode="aggregate", group_by=["country"])
    at_cap = await make_client(FakeCtGov(clone_studies(template, 20)), max_trials=20).fetch(plan, ["country"])
    over = await make_client(FakeCtGov(clone_studies(template, 21)), max_trials=20).fetch(plan, ["country"])
    assert at_cap.series[0].tier == 1 and len(at_cap.series[0].trials) == 20
    assert over.series[0].error_kind is FetchErrorKind.OVER_CAP


async def test_tier2_trials_the_buckets_miss_are_reported_as_unknown(load_fixture, template):
    counts = {**LUNG_PHASE_COUNTS, "Unknown": 3200}  # 12 trials in no bucket at all
    server = _count_server(load_fixture, "count_lung", template, _lung_bucket_totals(counts))
    [series] = (await make_client(server).fetch(PHASE_PLAN, ["phase"])).series
    assert series.bucket_counts["Unknown"] == 3212
    assert sum(series.bucket_counts.values()) == 14613


async def test_tier2_buckets_exceeding_the_total_are_refused(load_fixture, template):
    counts = {**LUNG_PHASE_COUNTS, "Phase 2": 9000}
    server = _count_server(load_fixture, "count_lung", template, _lung_bucket_totals(counts))
    [series] = (await make_client(server).fetch(PHASE_PLAN, ["phase"])).series
    assert series.error_kind is FetchErrorKind.UNAVAILABLE
    assert series.bucket_counts is None and series.trials == []


async def test_zero_results_fixture_needs_no_second_call(load_fixture):
    server = FakeCtGov(override=lambda r: httpx.Response(200, json=load_fixture("zero_results"))
                       if r.url.path.endswith("/studies") else None)
    result = await make_client(server).fetch(PHASE_PLAN, ["phase"])
    [series] = result.series
    assert (series.tier, series.trials_matched, series.trials, series.error) == (1, 0, [], None)
    assert len(server.study_requests()) == 1


# ---- count, cache, concurrency ------------------------------------------------------------------


async def test_count_asks_for_one_row_and_the_total(load_fixture):
    server = FakeCtGov(override=lambda r: httpx.Response(200, json=load_fixture("count_lung")))
    assert await make_client(server).count(PlanFilters(condition="lung cancer")) == 14613
    [request] = server.study_requests()
    assert dict(request.url.params) == {
        "countTotal": "true", "fields": "NCTId", "pageSize": "1", "query.cond": "lung cancer",
    }


async def test_repeating_a_fetch_is_served_from_the_cache(template):
    server = FakeCtGov(clone_studies(template, 5))
    client = make_client(server)
    first = await client.fetch(PHASE_PLAN, ["phase"])
    second = await client.fetch(PHASE_PLAN, ["phase"])
    assert (first.api_calls, first.cache_hits) == (3, 0)  # size check, data timestamp, one page
    assert (second.api_calls, second.cache_hits) == (0, 2)
    assert len(server.requests) == 3
    assert second.series[0].trials == first.series[0].trials


def _stamped_server(studies, stamp):
    """Returns a fake registry whose data timestamp can change, or can be not readable (None)."""
    def serve(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/version"):
            return httpx.Response(500) if stamp[0] is None else httpx.Response(200, json={"dataTimestamp": stamp[0]})
        return None

    return FakeCtGov(studies, override=serve)


async def test_cached_responses_live_until_the_registry_publishes_new_data(template):
    """A saved response is good until the data timestamp changes, not for one hour.

    Between two refreshes, the same request has the same answer. Thus the saved response is
    as accurate as a new one.
    """
    from tests.fakes import Sleeper

    stamp, sleeper = ["2026-10-09T09:00:05"], Sleeper()
    server = _stamped_server(clone_studies(template, 5), stamp)
    client = make_client(server, sleeper)
    await client.fetch(PHASE_PLAN, ["phase"])
    sleeper.now += 20 * 3600  # most of a day later, same registry data
    later = await client.fetch(PHASE_PLAN, ["phase"])
    assert later.cache_hits == 2 and len(server.study_requests()) == 2  # only the timestamp was re-read

    stamp[0] = "2026-10-10T09:00:04"  # the registry refreshed
    sleeper.now += 3600
    refreshed = await client.fetch(PHASE_PLAN, ["phase"])
    assert refreshed.cache_hits == 0 and len(server.study_requests()) == 4
    assert refreshed.data_timestamp == "2026-10-10T09:00:04"


async def test_without_a_readable_timestamp_the_cache_falls_back_to_a_lifetime(template):
    from tests.fakes import Sleeper

    sleeper = Sleeper()
    server = _stamped_server(clone_studies(template, 5), [None])
    client = make_client(server, sleeper, cache_ttl_seconds=3600)
    first = await client.fetch(PHASE_PLAN, ["phase"])
    assert first.data_timestamp is None and first.series[0].error is None
    sleeper.now += 3599
    assert (await client.fetch(PHASE_PLAN, ["phase"])).cache_hits == 2
    sleeper.now += 2
    assert (await client.fetch(PHASE_PLAN, ["phase"])).cache_hits == 0


async def test_the_timestamp_is_read_once_per_check_interval_not_per_request(template):
    server = FakeCtGov(clone_studies(template, 5))
    client = make_client(server)
    for condition in ("a", "b", "c", "d"):
        await client.count(PlanFilters(condition=condition))
    assert len(server.requests) - len(server.study_requests()) == 1


async def test_the_oldest_response_is_removed_when_the_cache_is_full(template):
    """The cache keeps a fixed number of responses in memory. The first search is fetched again
    after many other searches."""
    from app.ctgov.client import _MEMORY_ENTRIES

    server = FakeCtGov(clone_studies(template, 5))
    client = make_client(server)
    await client.fetch(PHASE_PLAN, ["phase"])
    for i in range(_MEMORY_ENTRIES):  # each search saves 2 responses: the size check and the page
        await client.fetch(QueryPlan(mode="aggregate", filters={"condition": f"c{i}"}, group_by=["phase"]), ["phase"])
    again = await client.fetch(PHASE_PLAN, ["phase"])
    assert again.cache_hits == 0 and again.api_calls == 2


async def test_cache_can_be_switched_off(template):
    server = FakeCtGov(clone_studies(template, 5))
    client = make_client(server, cache_ttl_seconds=0)
    await client.fetch(PHASE_PLAN, ["phase"])
    assert (await client.fetch(PHASE_PLAN, ["phase"])).cache_hits == 0


async def test_series_are_fetched_concurrently_but_never_more_than_four_at_once(template):
    studies = clone_studies(template, 3)
    in_flight = peak = 0

    async def serve(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return FakeCtGov(studies)(request)

    plan = QueryPlan(
        mode="aggregate",
        group_by=["phase"],
        series=[{"label": drug, "filters": {"drug": drug}} for drug in "ABCDE"],
    )
    result = await make_client(serve).fetch(plan, ["phase"])
    assert [s.label for s in result.series] == list("ABCDE")
    assert all(len(s.trials) == 3 for s in result.series)
    assert peak == 4
