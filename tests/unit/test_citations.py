"""TASK: Tests the citations: the trials shown, the evidence, and the search link.
GRADING: Bonus (deep citations).
"""
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from app.aggregate.aggregate import aggregate
from app.citations import build_citations, evidence_for, rank, ranking_rule, search_url
from app.ctgov.normalize import normalize_study
from app.models import QueryPlan, QueryRequest, SeriesFetch, Trial
from app.pipeline import Pipeline
from app.registry import bucket_expression
from tests.fakes import FakeCtGov, make_client

FIXTURES = ["pembrolizumab_page", "lung_recruiting", "phase12", "placebo_arms"]


def trial(number: int, **fields) -> Trial:
    return Trial(nct_id=f"NCT{number:08d}", **fields)


def ids(trials: list[Trial]) -> list[int]:
    return [int(t.nct_id[3:]) for t in trials]


def fetched(trials, label="All", filters=None, **extra) -> SeriesFetch:
    return SeriesFetch(label=label, filters=filters or {}, trials=trials, trials_matched=len(trials), tier=1, **extra)


def plan(*group_by, **extra) -> QueryPlan:
    return QueryPlan(mode="aggregate", group_by=list(group_by), **extra)


def value_at(study, indexed_path):
    """Follows a citation path such as `a.b[3].c` through the raw study JSON."""
    node = study
    for key, index in re.findall(r"([^.\[\]]+)(?:\[(\d+)\])?", indexed_path):
        node = node[key]
        if index:
            node = node[int(index)]
    return node


def term_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["term"][0]


# ---- which ranking applies ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "group_by", "rule"),
    [
        (["RECRUITING"], ["phase"], "recruiting"),
        (["RECRUITING"], ["start_year"], "recruiting"),  # the status filter wins over the time axis
        (["COMPLETED"], ["phase"], "completed"),
        (None, ["start_year"], "time_bucket"),
        (None, ["phase", "start_year"], "time_bucket"),
        (None, ["phase"], "default"),
        (["RECRUITING", "COMPLETED"], ["phase"], "default"),  # a mix is neither
        (["TERMINATED"], ["phase"], "default"),
    ],
)
def test_ranking_rule_follows_the_question(status, group_by, rule):
    series = fetched([], filters={"status": status} if status else {})
    assert ranking_rule(series, plan(*group_by)) == rule


# ---- the four rankings --------------------------------------------------------------------------

POOL = [
    trial(1, last_updated="2026-01-01", enrollment=50, has_results=False),
    trial(2, last_updated="2026-09-01", enrollment=10, has_results=False),
    trial(3, last_updated="2024-05-05", enrollment=900, has_results=True),
    trial(4, last_updated="2026-09-01", enrollment=300, has_results=False),
    trial(5, last_updated="2023-03-03", enrollment=20, has_results=True),
]


@pytest.mark.parametrize(
    ("rule", "expected", "why"),
    [
        ("recruiting", [4, 2, 1, 3, 5], "newest update first, then larger"),
        ("completed", [3, 5, 4, 1, 2], "with results first, then larger"),
        ("time_bucket", [3, 4, 1, 5, 2], "larger first, then with results"),
        ("default", [3, 5, 4, 1, 2], "with results, then larger, then newest"),
    ],
)
def test_each_ranking_rule(rule, expected, why):
    assert ids(rank(POOL, rule, 10)) == expected, why


def test_default_ranking_uses_the_update_date_as_the_last_tie_break():
    pool = [
        trial(1, has_results=True, enrollment=100, last_updated="2020-01-01"),
        trial(2, has_results=True, enrollment=100, last_updated="2025-01-01"),
    ]
    assert ids(rank(pool, "default", 10)) == [2, 1]


def test_time_bucket_ranking_breaks_enrollment_ties_by_results():
    pool = [trial(1, enrollment=100, has_results=False), trial(2, enrollment=100, has_results=True)]
    assert ids(rank(pool, "time_bucket", 10)) == [2, 1]


def test_missing_values_rank_last_and_full_ties_keep_a_fixed_order():
    pool = [trial(3), trial(1), trial(2, enrollment=5), trial(4, last_updated="2026-01-01")]
    assert ids(rank(pool, "default", 10)) == [2, 4, 1, 3]
    assert ids(rank(list(reversed(pool)), "default", 10)) == [2, 4, 1, 3]  # input order is irrelevant


# ---- sponsor cap and the limit ------------------------------------------------------------------


def test_at_most_two_trials_per_sponsor_while_other_sponsors_remain():
    pool = [trial(i, sponsor="Merck", enrollment=1000 - i) for i in range(1, 6)]
    pool += [trial(10, sponsor="Roche", enrollment=5), trial(11, sponsor="Pfizer", enrollment=4)]
    chosen = rank(pool, "default", 4)
    assert ids(chosen) == [1, 2, 10, 11]  # two Merck, then the best of the others


def test_sponsor_cap_ignores_case_and_treats_missing_sponsors_as_different():
    pool = [trial(1, sponsor="MERCK", enrollment=9), trial(2, sponsor="merck", enrollment=8), trial(3, sponsor="Merck", enrollment=7)]
    pool += [trial(4, enrollment=3), trial(5, enrollment=2), trial(6, enrollment=1)]
    assert ids(rank(pool, "default", 5)) == [1, 2, 4, 5, 6]


def test_slots_the_sponsor_cap_would_leave_empty_are_filled_with_the_next_best():
    """A bar with one sponsor only still shows the requested number of citations."""
    pool = [trial(i, sponsor="Merck", enrollment=100 - i) for i in range(1, 8)]
    assert ids(rank(pool, "default", 5)) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(("limit", "expected"), [(0, []), (1, [3]), (3, [3, 5, 4]), (50, [3, 5, 4, 1, 2])])
def test_items_are_capped_at_max_citations(limit, expected):
    assert ids(rank(POOL, "default", limit)) == expected


# ---- evidence -----------------------------------------------------------------------------------


def test_list_field_evidence_quotes_only_the_entry_that_matches_the_bar(load_fixture):
    study = load_fixture("placebo_arms")["studies"][0]  # 185 sites
    trial_ = normalize_study(study)
    [item] = evidence_for(trial_, "country", "Germany")
    assert item.value == "Germany"
    assert re.fullmatch(r"protocolSection\.contactsLocationsModule\.locations\[\d+\]\.country", item.field)
    assert value_at(study, item.field) == "Germany"


def test_combined_phase_evidence_is_the_raw_list_not_the_label(load_fixture):
    study = load_fixture("phase12")["studies"][0]
    [item] = evidence_for(normalize_study(study), "phase", "Phase 1/2")
    assert (item.field, item.value) == ("protocolSection.designModule.phases", ["PHASE1", "PHASE2"])


def test_labelled_field_evidence_is_the_raw_code(load_fixture):
    study = load_fixture("lung_recruiting")["studies"][0]
    [item] = evidence_for(normalize_study(study), "status", "Recruiting")
    assert (item.field, item.value) == ("protocolSection.statusModule.overallStatus", "RECRUITING")


def test_a_trial_in_the_unknown_bucket_cites_the_missing_field_as_null(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    study = next(s for s in studies if "phases" not in s["protocolSection"]["designModule"])
    [item] = evidence_for(normalize_study(study), "phase", "Unknown")
    assert (item.field, item.value) == ("protocolSection.designModule.phases", None)


# ---- bucket expressions (shared with the tier-2 counts) -----------------------------------------


@pytest.mark.parametrize(
    ("field", "label", "raw_values", "expression"),
    [
        ("phase", "Phase 1/2", (), "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2"),
        ("phase", "Phase 4", (), "AREA[Phase]PHASE4 AND NOT (AREA[Phase]EARLY_PHASE1 OR AREA[Phase]PHASE1 OR AREA[Phase]PHASE2 OR AREA[Phase]PHASE3 OR AREA[Phase]NA)"),
        ("phase", "Unknown", (), "AREA[Phase]MISSING"),
        ("status", "Active, not recruiting", (), "AREA[OverallStatus]ACTIVE_NOT_RECRUITING"),
        ("status", "Unknown status", (), "AREA[OverallStatus]UNKNOWN"),
        ("sponsor_class", "Industry", (), "AREA[LeadSponsorClass]INDUSTRY"),
        ("intervention_type", "Dietary supplement", (), "AREA[InterventionType]DIETARY_SUPPLEMENT"),
        ("start_year", 2019, (), "AREA[StartDate]RANGE[2019-01-01,2019-12-31]"),
        ("has_results", "Yes", (), "AREA[HasResults]true"),
        ("has_results", "No", (), "AREA[HasResults]false"),
        ("country", "Germany", ("Germany",), 'AREA[LocationCountry]COVERAGE[FullMatch]"Germany"'),
        ("country", "Germany", (), 'AREA[LocationCountry]COVERAGE[FullMatch]"Germany"'),
        (
            # one phrase search for the shortest spelling also finds "Pfizer Inc."
            "sponsor", "Pfizer", ("Pfizer Inc.", "Pfizer", "PFIZER"), 'AREA[LeadSponsorName]"Pfizer"',
        ),
        (
            "sponsor", "Merck", ("Merck", "Merck KGaA"),
            '(AREA[LeadSponsorName]"Merck" OR AREA[LeadSponsorName]"Merck KGaA")',  # different companies' names stay apart
        ),
        (
            "condition", "Non Small Cell Lung Cancer", ("Non Small Cell Lung Cancer", "Non-small Cell Lung Cancer"),
            'AREA[Condition]COVERAGE[FullMatch]"Non Small Cell Lung Cancer"',
        ),
        ("sponsor", "Bristol-Myers Squibb", ("Bristol-Myers Squibb",), 'AREA[LeadSponsorName]"Bristol-Myers Squibb"'),
        ("condition", 'Say "ah"', ('Say "ah"',), 'AREA[Condition]COVERAGE[FullMatch]"Say  ah"'),
        ("status", "A label nobody defined", (), None),
        ("enrollment", "0–50", (), None),
        ("duration_months", "0–12", (), None),
    ],
)
def test_bucket_expression(field, label, raw_values, expression):
    assert bucket_expression(field, label, raw_values) == expression


# ---- search link --------------------------------------------------------------------------------


def test_search_url_carries_the_filters_to_the_website_boxes():
    url = search_url({"query.cond": "lung cancer", "query.intr": "pembrolizumab", "query.spons": "Merck"}, [])
    assert url == "https://clinicaltrials.gov/search?cond=lung+cancer&intr=pembrolizumab&spons=Merck"


def test_search_url_puts_status_other_filters_and_the_bucket_into_other_terms():
    params = {
        "query.cond": "melanoma",
        "filter.overallStatus": "RECRUITING,COMPLETED",
        "filter.advanced": 'AREA[LocationCountry]"Germany" AND AREA[StartDate]RANGE[2020-01-01,MAX]',
    }
    url = search_url(params, ["AREA[Phase]PHASE1 AND AREA[Phase]PHASE2", None])
    assert parse_qs(urlparse(url).query)["cond"] == ["melanoma"]
    assert term_of(url) == (
        "(AREA[OverallStatus]RECRUITING OR AREA[OverallStatus]COMPLETED) AND "
        'AREA[LocationCountry]"Germany" AND AREA[StartDate]RANGE[2020-01-01,MAX] AND '
        "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2"
    )


def test_search_url_with_nothing_to_search_for_is_the_plain_search_page():
    assert search_url({}, [None]) == "https://clinicaltrials.gov/search"


# ---- whole citation sets on real trials ---------------------------------------------------------


def _cite(studies, the_plan, max_citations=10, **series_extra):
    series = fetched([normalize_study(s) for s in studies], **series_extra)
    result = aggregate([series], the_plan)
    return result, build_citations(result, [series], the_plan, max_citations)


@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.parametrize("group_by", [["phase"], ["country"], ["start_year"], ["sponsor"], ["status", "intervention_type"]])
def test_every_datum_has_a_citation_set_whose_total_is_the_bar(load_fixture, fixture, group_by):
    result, citations = _cite(load_fixture(fixture)["studies"], plan(*group_by, limit=50))
    assert set(citations) == {row["id"] for row in result.rows}
    for row in result.rows:
        cited = citations[row["id"]]
        assert cited.total == row["trial_count"]
        assert cited.shown == len(cited.items) == min(cited.total, 10)
        assert cited.is_sample is False
        assert cited.search_url.startswith("https://clinicaltrials.gov/search")


@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.parametrize("group_by", [["phase"], ["country"], ["start_year"], ["sponsor"], ["condition"], ["status", "intervention_type"]])
def test_evidence_values_equal_the_raw_fixture_values_byte_for_byte(load_fixture, fixture, group_by):
    studies = load_fixture(fixture)["studies"]
    raw = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in studies}
    _, citations = _cite(studies, plan(*group_by, limit=50), max_citations=50)
    checked = 0
    for cited in citations.values():
        for item in cited.items:
            assert item.url == f"https://clinicaltrials.gov/study/{item.nct_id}"
            assert len(item.evidence) >= len(group_by)
            for evidence in item.evidence:
                if evidence.value is None:  # the Unknown bucket: the field really is absent
                    with pytest.raises((KeyError, IndexError)):
                        value_at(raw[item.nct_id], evidence.field)
                    continue
                found = value_at(raw[item.nct_id], evidence.field)
                assert json.dumps(found) == json.dumps(evidence.value)
                checked += 1
    assert checked > 0


def test_a_country_bar_cites_trials_that_really_have_a_site_there(load_fixture):
    studies = load_fixture("lung_recruiting")["studies"]
    raw = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in studies}
    result, citations = _cite(studies, plan("country", limit=50), max_citations=50)
    for row in result.rows:
        for item in citations[row["id"]].items:
            sites = raw[item.nct_id]["protocolSection"].get("contactsLocationsModule", {}).get("locations", [])
            if row["country"] == "Unknown":
                assert sites == []
            else:
                assert row["country"] in {site["country"] for site in sites}
                assert [e.value for e in item.evidence] == [row["country"]]
            assert item.bucket_label == row["country"]


def test_two_fields_give_two_lines_of_evidence_and_a_combined_label(load_fixture):
    result, citations = _cite(load_fixture("pembrolizumab_page")["studies"], plan("phase", "sponsor_class"))
    row = next(r for r in result.rows if r["trial_count"])
    item = citations[row["id"]].items[0]
    assert item.bucket_label == f'{row["phase"]} · {row["sponsor_class"]}'
    assert [e.field.rsplit(".", 1)[1] for e in item.evidence] == ["phases", "class"]


def test_search_url_of_a_bar_narrows_the_series_search_to_that_bar(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    result, citations = _cite(studies, plan("phase"), api_params={"query.intr": "pembrolizumab"})
    by_phase = {row["phase"]: citations[row["id"]].search_url for row in result.rows}
    assert parse_qs(urlparse(by_phase["Phase 1/2"]).query) == {
        "intr": ["pembrolizumab"], "term": ["AREA[Phase]PHASE1 AND AREA[Phase]PHASE2"],
    }
    assert term_of(by_phase["Unknown"]) == "AREA[Phase]MISSING"


def test_merged_sponsor_spellings_are_all_in_the_search_link():
    path = "protocolSection.sponsorCollaboratorsModule.leadSponsor.name"
    trials = [
        trial(i, sponsor=name, evidence={"sponsor": [{"path": path, "raw": name, "label": name}]})
        for i, name in enumerate(["Pfizer", "Pfizer Inc.", "Pfizer"], start=1)
    ]
    series = fetched(trials)
    result = aggregate([series], plan("sponsor"))
    [cited] = build_citations(result, [series], plan("sponsor"), 10).values()
    assert cited.total == 3
    assert term_of(cited.search_url) == 'AREA[LeadSponsorName]"Pfizer"'
    assert cited.search_is_exact is False  # a name search can list a few more trials than the bar


def test_a_bar_with_no_trials_still_has_a_citation_set(load_fixture):
    a = fetched([trial(1, phase="Phase 1")], "A")
    b = fetched([trial(2, phase="Phase 2")], "B")
    two = QueryPlan(mode="aggregate", group_by=["phase"], series=[{"label": "A"}, {"label": "B"}])
    result = aggregate([a, b], two)
    citations = build_citations(result, [a, b], two, 10)
    empty = [citations[row["id"]] for row in result.rows if row["trial_count"] == 0]
    assert len(empty) == 2
    assert all((c.total, c.shown, c.items) == (0, 0, []) for c in empty)


def test_series_rows_cite_only_trials_of_their_own_series():
    a = fetched([trial(1, phase="Phase 2")], "Drug A", api_params={"query.intr": "A"})
    b = fetched([trial(2, phase="Phase 2")], "Drug B", api_params={"query.intr": "B"})
    two = QueryPlan(mode="aggregate", group_by=["phase"], series=[{"label": "Drug A"}, {"label": "Drug B"}])
    result = aggregate([a, b], two)
    citations = build_citations(result, [a, b], two, 10)
    for row in result.rows:
        cited = citations[row["id"]]
        expected_id, drug = ("NCT00000001", "A") if row["series"] == "Drug A" else ("NCT00000002", "B")
        assert [item.nct_id for item in cited.items] == [expected_id]
        assert cited.items[0].bucket_label == f'{row["series"]} · Phase 2'
        assert parse_qs(urlparse(cited.search_url).query)["intr"] == [drug]


def test_max_citations_zero_keeps_totals_and_links_but_lists_no_trials(load_fixture):
    result, citations = _cite(load_fixture("pembrolizumab_page")["studies"], plan("phase"), max_citations=0)
    assert sum(c.total for c in citations.values()) == 50
    assert all(c.shown == 0 and c.items == [] and c.search_url for c in citations.values())


@pytest.mark.parametrize(
    ("group_by", "exact"),
    [
        (["phase"], True), (["status"], True), (["sponsor_class"], True), (["start_year"], True),
        (["has_results"], True), (["phase", "status"], True),
        (["country"], False), (["sponsor"], False), (["condition"], False), (["intervention"], False),
        (["phase", "country"], False),  # one loose part makes the whole link loose
    ],
)
def test_search_link_says_whether_it_is_exact(load_fixture, group_by, exact):
    """Fixed codes and years have an exact search. ClinicalTrials.gov matches names loosely."""
    result, citations = _cite(load_fixture("pembrolizumab_page")["studies"], plan(*group_by, limit=50))
    known = [citations[r["id"]] for r in result.rows if "Unknown" not in r.values()]
    assert known and all(c.search_is_exact is exact for c in known)


def test_the_missing_value_bucket_is_always_searched_exactly(load_fixture):
    result, citations = _cite(load_fixture("pembrolizumab_page")["studies"], plan("country", limit=50))
    [unknown] = [citations[r["id"]] for r in result.rows if r["country"] == "Unknown"]
    assert unknown.search_is_exact is True
    assert term_of(unknown.search_url) == "AREA[LocationCountry]MISSING"


def test_a_numeric_bin_link_is_marked_inexact_because_it_shows_the_whole_series(load_fixture):
    binned = plan("enrollment", bin={"field": "enrollment", "bin_width": 100})
    result, citations = _cite(load_fixture("pembrolizumab_page")["studies"], binned, api_params={"query.intr": "pembrolizumab"})
    cited = citations[result.rows[0]["id"]]
    assert cited.search_is_exact is False
    assert cited.search_url == "https://clinicaltrials.gov/search?intr=pembrolizumab"


def test_a_merged_bar_cites_each_trial_with_its_own_spelling(load_fixture):
    """The bar shows "Non Small Cell Lung Cancer".

    A trial that wrote the name with hyphens is also cited, and the citation quotes the text
    of its own record.
    """
    path = "protocolSection.conditionsModule.conditions[0]"
    spellings = ["Non Small Cell Lung Cancer", "Non Small Cell Lung Cancer", "Non-small Cell Lung Cancer"]
    trials = [
        trial(i, condition=[text], evidence={"condition": [{"path": path, "raw": text, "label": text}]})
        for i, text in enumerate(spellings, start=1)
    ]
    series = fetched(trials)
    result = aggregate([series], plan("condition"))
    [cited] = build_citations(result, [series], plan("condition"), 10).values()
    assert cited.total == 3
    assert sorted(item.evidence[0].value for item in cited.items) == sorted(spellings)
    assert all(item.bucket_label == "Non Small Cell Lung Cancer" for item in cited.items)
    assert term_of(cited.search_url) == 'AREA[Condition]COVERAGE[FullMatch]"Non Small Cell Lung Cancer"'


# ---- tier 2 -------------------------------------------------------------------------------------


def test_tier2_citations_are_marked_as_samples_and_keep_the_exact_total():
    sample = [trial(1, phase="Phase 2"), trial(2, phase="Phase 2"), trial(3, phase="Phase 3")]
    series = SeriesFetch(
        label="All", filters={}, trials=sample, trials_matched=5236, tier=2, is_sample=True,
        bucket_field="phase", bucket_counts={"Phase 2": 4004, "Phase 3": 1232},
        api_params={"query.cond": "lung cancer"},
    )
    result = aggregate([series], plan("phase", sort="natural"))
    citations = build_citations(result, [series], plan("phase"), 10)
    phase2, phase3 = (citations[row["id"]] for row in result.rows)
    assert (phase2.total, phase2.shown, phase2.is_sample) == (4004, 2, True)
    assert (phase3.total, phase3.shown, phase3.is_sample) == (1232, 1, True)
    assert term_of(phase3.search_url).startswith("AREA[Phase]PHASE3 AND NOT (")


# ---- through the pipeline -----------------------------------------------------------------------


async def test_response_has_one_citation_set_per_data_point(load_fixture):
    studies = load_fixture("lung_recruiting")["studies"]
    raw = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in studies}
    the_plan = QueryPlan(mode="aggregate", filters={"condition": "lung cancer", "status": ["RECRUITING"]}, group_by=["country"], limit=5)
    request = QueryRequest(query="countries with most recruiting lung cancer trials", max_citations=3)
    response = await Pipeline(make_client(FakeCtGov(studies))).run_plan(request, the_plan)

    body = json.loads(response.model_dump_json())
    assert set(body["citations"]) == {row["id"] for row in body["visualization"]["data"]}
    for row in body["visualization"]["data"]:
        cited = body["citations"][row["id"]]
        assert cited["total"] == row["trial_count"] and cited["shown"] == min(3, cited["total"])
        assert "AREA[OverallStatus]RECRUITING" in term_of(cited["search_url"])
        assert f'AREA[LocationCountry]COVERAGE[FullMatch]"{row["country"]}"' in term_of(cited["search_url"])
        for item in cited["items"]:
            assert set(item) == {"nct_id", "title", "url", "bucket_label", "evidence"}
            assert item["title"] == raw[item["nct_id"]]["protocolSection"]["identificationModule"]["briefTitle"]
            [evidence] = item["evidence"]
            assert value_at(raw[item["nct_id"]], evidence["field"]) == evidence["value"] == row["country"]

    # the recruiting ranking: most recently updated record first
    top = body["citations"][body["visualization"]["data"][0]["id"]]["items"]
    updates = [raw[i["nct_id"]]["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"]["date"] for i in top]
    assert updates[0] == max(updates)
