"""TASK: Tests the network graph: pairs, placebo removal, and pruning.
GRADING: Coverage (meaningful network graphs).
"""
import json

import pytest

from app.aggregate.labels import NameLabeler
from app.aggregate.network import _Side, network, plural
from app.citations import build_citations
from app.config import Settings
from app.ctgov.normalize import normalize_study
from app.models import ChartType, InterventionRecord, NetworkGraph, QueryPlan, QueryRequest, SeriesFetch, Trial
from app.pipeline import Pipeline
from app.spec_builder import build_visualization, choose_chart
from app.validator import validate
from tests.fakes import FakeCtGov, FakeLLM, llm_text, make_client

DRUGS = QueryPlan(mode="network", network={"source_field": "intervention", "target_field": "intervention"})
SPONSOR_DRUG = QueryPlan(mode="network", network={"source_field": "sponsor", "target_field": "intervention"})


def drug(name: str, kind: str = "DRUG", arms: tuple = ("EXPERIMENTAL",)) -> InterventionRecord:
    return InterventionRecord(name=name, type=kind, arm_types=list(arms))


def trial(number: int, *drugs, **fields) -> Trial:
    records = [d if isinstance(d, InterventionRecord) else drug(d) for d in drugs]
    return Trial(nct_id=f"NCT{number:08d}", intervention=[r.name for r in records], intervention_records=records, **fields)


def fetched(trials, **extra) -> SeriesFetch:
    return SeriesFetch(label="All", filters={}, trials=trials, trials_matched=len(trials), tier=1, **extra)


def graph(result):
    names = {n.id: n.label for n in result.network.nodes}
    nodes = {n.label: n.trial_count for n in result.network.nodes}
    edges = {tuple(sorted((names[e.source], names[e.target]))): e.shared_trials for e in result.network.edges}
    return nodes, edges


def scripted_labeler(tmp_path, **labels) -> tuple[NameLabeler, FakeLLM]:
    body = json.dumps({"labels": [{"name": name, "label": label} for name, label in labels.items()]})
    llm = FakeLLM(llm_text(body))
    return NameLabeler(llm=llm, settings=Settings(_env_file=None), cache_path=tmp_path / "labels.json"), llm


# ---- acceptance: NCT04003636 --------------------------------------------------------------------


async def test_placebo_trial_keeps_gemcitabine_and_cisplatin_and_drops_the_placebo(load_fixture):
    """A placebo has the type DRUG, so the type cannot remove it. The arm rule removes it.

    Gemcitabine and cisplatin are also given in the placebo arm, but not only there. Thus they stay.
    """
    study = load_fixture("placebo_arms")["studies"][0]
    assert {(i["name"], i["type"]) for i in study["protocolSection"]["armsInterventionsModule"]["interventions"]} >= {("Placebo", "DRUG")}
    result = await network([fetched([normalize_study(study)])], DRUGS)
    nodes, edges = graph(result)
    assert nodes == {"Pembrolizumab": 1, "Gemcitabine": 1, "Cisplatin": 1}
    assert edges == {("Cisplatin", "Gemcitabine"): 1, ("Cisplatin", "Pembrolizumab"): 1, ("Gemcitabine", "Pembrolizumab"): 1}
    assert result.dropped == {"intervention: control_arm_only": 1}
    assert result.notes == ["Only specific drugs are shown. Names left out: 1 given only in placebo or sham arms."]


# ---- layers 1-3 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind", "arms", "layer"),
    [
        ("Placebo", "DRUG", ["PLACEBO_COMPARATOR"], "control_arm_only"),
        ("Sham injection", "DRUG", ["SHAM_COMPARATOR", "PLACEBO_COMPARATOR"], "control_arm_only"),
        ("Gemcitabine", "DRUG", ["EXPERIMENTAL", "PLACEBO_COMPARATOR"], None),  # not only in the control arm
        ("Saline flush", "DRUG", ["PLACEBO_COMPARATOR", None], "stop_list"),  # an untyped arm is not proof; the name is
        ("Dexamethasone", "DRUG", [], None),  # no arm information at all
        ("Radiotherapy", "RADIATION", ["EXPERIMENTAL"], "not_drug_or_biological"),
        ("Biopsy", "PROCEDURE", ["EXPERIMENTAL"], "not_drug_or_biological"),
        ("Unknown thing", None, ["EXPERIMENTAL"], "not_drug_or_biological"),
        ("Nivolumab", "BIOLOGICAL", ["EXPERIMENTAL"], None),
        ("Matching Placebo", "DRUG", ["EXPERIMENTAL"], "stop_list"),  # placebo inside an active arm
        ("Vehicle cream", "DRUG", ["ACTIVE_COMPARATOR"], "stop_list"),
        ("Standard of Care chemotherapy", "DRUG", ["ACTIVE_COMPARATOR"], "stop_list"),
        ("Best Supportive Care", "DRUG", ["ACTIVE_COMPARATOR"], "stop_list"),
        ("Shampoo with ketoconazole", "DRUG", ["EXPERIMENTAL"], None),  # "sham" must be a whole word
    ],
)
def test_which_layer_removes_a_record(name, kind, arms, layer):
    assert _Side._removed_by(name, kind, arms) == layer


async def test_layers_are_counted_once_per_name_in_the_order_they_apply():
    trials = [
        trial(1, "Temozolomide", "Lomustine", drug("Placebo", arms=("PLACEBO_COMPARATOR",)), drug("Surgery", "PROCEDURE")),
        trial(2, "Temozolomide", "Lomustine", drug("placebo", arms=("PLACEBO_COMPARATOR",)), drug("Normal saline")),
    ]
    result = await network([fetched(trials)], DRUGS)
    assert graph(result) == ({"Temozolomide": 2, "Lomustine": 2}, {("Lomustine", "Temozolomide"): 2})
    assert result.dropped == {
        "intervention: control_arm_only": 1,  # "Placebo" and "placebo" are one name
        "intervention: not_drug_or_biological": 1,
        "intervention: stop_list": 1,
    }
    assert result.notes[0] == (
        "Only specific drugs are shown. Names left out: 1 given only in placebo or sham arms; "
        "1 not typed as a drug or biological; 1 named as a placebo, sham, vehicle, saline or standard care."
    )


async def test_a_name_kept_in_one_trial_is_not_reported_as_removed_because_of_another():
    """Dexamethasone is the control in trial 1 and an active drug in trial 2. It is a real drug."""
    trials = [
        trial(1, "Bortezomib", drug("Dexamethasone", arms=("PLACEBO_COMPARATOR",))),
        trial(2, "Bortezomib", "Dexamethasone"),
    ]
    result = await network([fetched(trials)], DRUGS)
    assert graph(result) == ({"Bortezomib": 2, "Dexamethasone": 1}, {("Bortezomib", "Dexamethasone"): 1})
    assert result.dropped == {} and result.notes == []


# ---- pairs --------------------------------------------------------------------------------------


async def test_co_occurrence_counts_every_unordered_pair_once_per_trial():
    trials = [trial(1, "A", "B", "C"), trial(2, "B", "A"), trial(3, "C")]
    nodes, edges = graph(await network([fetched(trials)], DRUGS))
    assert nodes == {"A": 2, "B": 2, "C": 2}  # C's second trial has no partner but still counts for the node
    assert edges == {("A", "B"): 2, ("A", "C"): 1, ("B", "C"): 1}


async def test_two_fields_link_each_source_to_each_target():
    trials = [
        trial(1, "Nivolumab", "Ipilimumab", sponsor="BMS"),
        trial(2, "Nivolumab", sponsor="BMS"),
        trial(3, "Pembrolizumab", sponsor="Merck"),
        trial(4, "Pembrolizumab"),  # no sponsor: no "Unknown" node is invented
    ]
    result = await network([fetched(trials)], SPONSOR_DRUG)
    nodes, edges = graph(result)
    assert nodes == {"BMS": 2, "Merck": 1, "Nivolumab": 2, "Ipilimumab": 1, "Pembrolizumab": 2}
    assert edges == {("BMS", "Nivolumab"): 2, ("BMS", "Ipilimumab"): 1, ("Merck", "Pembrolizumab"): 1}
    kinds = {n.label: n.type for n in result.network.nodes}
    assert kinds["BMS"] == "sponsor" and kinds["Nivolumab"] == "intervention"
    assert "Unknown" not in nodes


async def test_a_sponsor_and_a_drug_with_the_same_name_are_different_nodes():
    result = await network([fetched([trial(1, "Novartis", sponsor="Novartis")])], SPONSOR_DRUG)
    assert sorted((n.label, n.type) for n in result.network.nodes) == [("Novartis", "intervention"), ("Novartis", "sponsor")]
    assert len(result.network.edges) == 1


async def test_spellings_of_one_drug_are_one_node():
    trials = [trial(1, "Temozolomide", "bevacizumab"), trial(2, "temozolomide", "Bevacizumab"), trial(3, "TEMOZOLOMIDE", "Bevacizumab")]
    result = await network([fetched(trials)], DRUGS)
    assert graph(result) == ({"Bevacizumab": 3, "Temozolomide": 3}, {("Bevacizumab", "Temozolomide"): 3})
    assert result.notes == [
        "Intervention names written in slightly different ways are counted together and shown under the most common spelling."
    ]


async def test_a_network_of_other_fields_needs_no_drug_cleaning():
    trials = [trial(i, sponsor="BMS", country=["United States", "Japan"]) for i in range(2)]
    plan = QueryPlan(mode="network", network={"source_field": "sponsor", "target_field": "country"})
    result = await network([fetched(trials)], plan)
    assert graph(result)[1] == {("BMS", "United States"): 2, ("BMS", "Japan"): 2}
    assert result.dropped == {} and result.notes == []


# ---- pruning ------------------------------------------------------------------------------------


def _many_trials(pairs: dict[tuple[str, str], int], filler: int = 0) -> list[Trial]:
    trials, n = [], 0
    for (a, b), count in pairs.items():
        for _ in range(count):
            n += 1
            trials.append(trial(n, a, b))
    return trials + [trial(1000 + i, f"Solo{i}") for i in range(filler)]


async def test_only_the_strongest_edges_are_kept_and_the_message_says_so():
    pairs = {(f"A{i}", f"B{i}"): 20 - i for i in range(15)}  # weights 20 down to 6
    plan = QueryPlan(mode="network", network={"source_field": "intervention", "target_field": "intervention", "max_edges": 5})
    result = await network([fetched(_many_trials(pairs))], plan)
    assert sorted(e.shared_trials for e in result.network.edges) == [16, 17, 18, 19, 20]
    assert result.pruning.model_dump() == {
        "edges_shown": 5,
        "edges_total": 15,
        "min_shared_trials": 16,
        "message": "Showing the 5 strongest connections out of 15. Each line means the two drugs were used together "
        "in at least 16 trials. Connections seen in only one trial are left out.",
    }
    assert len(result.network.nodes) == 10  # nodes without a shown edge are gone


async def test_single_trial_connections_are_dropped_in_a_large_dataset_but_kept_in_a_small_one():
    pairs = {("A", "B"): 3, ("C", "D"): 1}
    large = await network([fetched(_many_trials(pairs, filler=60))], DRUGS)
    assert graph(large)[1] == {("A", "B"): 3}
    assert "D" not in graph(large)[0] and not any(n.label.startswith("Solo") for n in large.network.nodes)
    assert large.pruning.edges_total == 2 and large.pruning.min_shared_trials == 3

    small = await network([fetched(_many_trials(pairs))], DRUGS)  # 4 trials: every connection is worth showing
    assert graph(small)[1] == {("A", "B"): 3, ("C", "D"): 1}
    assert small.pruning.message == "Showing all 2 connections. Each line means the two drugs were used together in at least 1 trial."


async def test_when_every_connection_is_a_single_trial_nothing_is_drawn_and_that_is_said():
    pairs = {(f"A{i}", f"B{i}"): 1 for i in range(60)}
    result = await network([fetched(_many_trials(pairs))], DRUGS)
    assert result.network.nodes == [] and result.network.edges == []
    assert result.pruning.message == "60 connections were found, but each appears in a single trial only, so none is shown."


async def test_no_trials_gives_an_empty_network():
    result = await network([fetched([])], DRUGS)
    assert result.network.nodes == [] and result.pruning.message == "No connections were found among these trials."


async def test_edges_and_nodes_come_out_strongest_first_with_stable_ids():
    pairs = {("A", "B"): 5, ("A", "C"): 9, ("B", "C"): 7}
    result = await network([fetched(_many_trials(pairs))], DRUGS)
    assert [(e.id, e.shared_trials) for e in result.network.edges] == [("e1", 9), ("e2", 7), ("e3", 5)]
    assert [(n.id, n.label, n.trial_count) for n in result.network.nodes] == [("n1", "C", 16), ("n2", "A", 14), ("n3", "B", 12)]
    assert {e.source for e in result.network.edges} | {e.target for e in result.network.edges} == {"n1", "n2", "n3"}


def test_bipartite_message_reads_naturally():
    from app.aggregate.network import _pruning

    info = _pruning(60, 4322, [5] * 60, 2, _Side("sponsor"), _Side("intervention"), False)
    assert info.message == (
        "Showing the 60 strongest connections out of 4,322. Each line means the sponsor ran at least "
        "5 trials involving the drug. Connections seen in only one trial are left out."
    )


@pytest.mark.parametrize(("noun", "many"), [("drug", "drugs"), ("country", "countries"), ("status", "statuses"), ("sponsor category", "sponsor categories")])
def test_plural(noun, many):
    assert plural(noun) == many


# ---- layer 4: the label model -------------------------------------------------------------------


async def test_names_the_model_calls_not_a_specific_drug_are_removed(tmp_path):
    pairs = {("Temozolomide", "Chemotherapy"): 4, ("Temozolomide", "Lomustine"): 3, ("Lomustine", "Chemotherapy"): 2}
    labeler, llm = scripted_labeler(tmp_path, Temozolomide="specific_drug", Lomustine="specific_drug", Chemotherapy="unspecific_care")
    result = await network([fetched(_many_trials(pairs))], DRUGS, labeler)
    assert graph(result) == ({"Temozolomide": 7, "Lomustine": 5}, {("Lomustine", "Temozolomide"): 3})
    assert result.dropped == {"intervention: model_label": 1}
    assert result.notes == ["Only specific drugs are shown. Names left out: 1 judged not to be a specific drug."]
    assert result.trace["labels"] == {
        "checked": 3, "from_cache": 0, "asked_model": 3, "rejected_answers": [], "removed": ["Chemotherapy"], "error": None,
    }
    assert result.pruning.edges_total == 1  # edges to removed names are not counted as "left out"


async def test_a_name_the_model_invents_cannot_remove_or_add_anything(tmp_path):
    pairs = {("Temozolomide", "Lomustine"): 3}
    labeler, _ = scripted_labeler(tmp_path, Temozolomide="specific_drug", Lomustine="specific_drug", Bevacizumab="not_a_drug")
    result = await network([fetched(_many_trials(pairs))], DRUGS, labeler)
    assert graph(result)[0] == {"Temozolomide": 3, "Lomustine": 3}
    assert result.trace["labels"]["rejected_answers"] == ["Bevacizumab"]


async def test_a_name_the_model_does_not_answer_for_is_kept(tmp_path):
    labeler, _ = scripted_labeler(tmp_path, Temozolomide="specific_drug")
    result = await network([fetched(_many_trials({("Temozolomide", "Mystery-17"): 3}))], DRUGS, labeler)
    assert set(graph(result)[0]) == {"Temozolomide", "Mystery-17"}


async def test_only_names_that_could_be_drawn_are_sent_to_the_model(tmp_path):
    strong = {(f"A{i}", f"B{i}"): 50 - i for i in range(4)}
    weak = {(f"X{i}", f"Y{i}"): 2 for i in range(30)}
    plan = QueryPlan(mode="network", network={"source_field": "intervention", "target_field": "intervention", "max_edges": 2})
    labeler, llm = scripted_labeler(tmp_path)
    await network([fetched(_many_trials({**strong, **weak}))], plan, labeler)
    sent = json.loads(llm.requests[0]["input"][0]["content"])["names"]
    assert sorted(sent) == ["A0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]  # twice max_edges edges, not all 68 names
    assert len(llm.requests) == 1


async def test_only_drug_names_are_sent_never_sponsors(tmp_path):
    labeler, llm = scripted_labeler(tmp_path)
    trials = [trial(i, "Nivolumab", sponsor="BMS") for i in range(3)]
    await network([fetched(trials)], SPONSOR_DRUG, labeler)
    assert json.loads(llm.requests[0]["input"][0]["content"]) == {"names": ["Nivolumab"]}


async def test_label_model_down_still_gives_a_network_with_a_warning(tmp_path):
    labeler = NameLabeler(llm=FakeLLM(ValueError("no connection")), settings=Settings(_env_file=None), cache_path=tmp_path / "c.json")
    result = await network([fetched(_many_trials({("Temozolomide", "Chemotherapy"): 3}))], DRUGS, labeler)
    assert set(graph(result)[0]) == {"Temozolomide", "Chemotherapy"}
    assert result.notes == [
        "The names could not be checked by the language model, so a few entries that are not specific drugs may remain."
    ]
    assert "no connection" in result.trace["labels"]["error"]


async def test_without_a_labeler_layers_1_to_3_still_apply():
    trials = [trial(i, "Temozolomide", "Chemotherapy", drug("Placebo", arms=("PLACEBO_COMPARATOR",))) for i in range(3)]
    result = await network([fetched(trials)], DRUGS)
    assert set(graph(result)[0]) == {"Temozolomide", "Chemotherapy"} and "labels" not in result.trace


# ---- citations, chart, validator, pipeline ------------------------------------------------------


async def test_nodes_cite_one_line_and_edges_two(load_fixture):
    studies = load_fixture("pembrolizumab_page")["studies"]
    raw = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in studies}
    series = fetched([normalize_study(s) for s in studies], api_params={"query.intr": "pembrolizumab"})
    result = await network([series], SPONSOR_DRUG)
    citations = build_citations(result, [series], SPONSOR_DRUG, 3)
    assert set(citations) == {n.id for n in result.network.nodes} | {e.id for e in result.network.edges}

    names = {n.id: n for n in result.network.nodes}
    for edge in result.network.edges:
        cited = citations[edge.id]
        assert cited.total == edge.shared_trials and cited.shown == min(3, cited.total)
        for item in cited.items:
            sponsor_line, drug_line = item.evidence
            assert sponsor_line.field == "protocolSection.sponsorCollaboratorsModule.leadSponsor.name"
            assert drug_line.field.startswith("protocolSection.armsInterventionsModule.interventions[")
            interventions = raw[item.nct_id]["protocolSection"]["armsInterventionsModule"]["interventions"]
            assert drug_line.value in [i["name"] for i in interventions]
            assert item.bucket_label == f"{names[edge.source].label} — {names[edge.target].label}"
    for node in result.network.nodes:
        cited = citations[node.id]
        assert cited.total == node.trial_count and all(len(item.evidence) == 1 for item in cited.items)
        assert cited.search_is_exact is False  # a name search


async def test_network_chart_spec(load_fixture):
    plan = QueryPlan.model_validate({**SPONSOR_DRUG.model_dump(), "filters": {"condition": "glioblastoma"}})
    result = await network([fetched([trial(1, "Temozolomide", sponsor="NCI")])], plan)
    chart, note = choose_chart(plan, result, None)
    viz = build_visualization(plan, result, chart)
    assert isinstance(viz, NetworkGraph) and note is None
    assert viz.title == "Glioblastoma trials: sponsors and drugs"
    body = viz.model_dump(mode="json")
    assert body["encoding"]["nodes"]["size"] == {"field": "trial_count", "type": "quantitative", "title": "Trials", "unit": "trials"}
    assert body["encoding"]["nodes"]["color"]["field"] == "type"
    assert body["encoding"]["edges"] == {
        "source": "source", "target": "target",
        "weight": {"field": "shared_trials", "type": "quantitative", "title": "Shared trials", "unit": "trials"},
    }
    assert body["data"]["nodes"][0].keys() == {"id", "label", "type", "trial_count"}
    assert body["data"]["edges"][0].keys() == {"id", "source", "target", "shared_trials"}


async def test_co_occurrence_title_and_chart_override_note():
    plan = QueryPlan.model_validate({**DRUGS.model_dump(), "filters": {"condition": "glioblastoma"}})
    result = await network([fetched([trial(1, "A", "B")])], plan)
    assert build_visualization(plan, result, ChartType.NETWORK_GRAPH).title == "Glioblastoma trials: drugs used together"
    assert choose_chart(plan, result, ChartType.BAR_CHART) == (
        ChartType.NETWORK_GRAPH,
        "A bar chart was asked for, but links between drugs and drugs are shown as a network graph, which fits this data.",
    )


def test_a_network_takes_one_series_and_categorical_fields():
    two = QueryPlan.model_validate({**DRUGS.model_dump(), "series": [{"label": "A", "filters": {"drug": "A"}}, {"label": "B", "filters": {"drug": "B"}}]})
    assert [i.message for i in validate(two)] == ["A network shows one series, not a comparison."]
    titles = QueryPlan(mode="network", network={"source_field": "brief_title", "target_field": "sponsor"})
    assert validate(titles)[0].path == "network.source_field"


async def test_network_through_the_pipeline(load_fixture, tmp_path):
    studies = load_fixture("pembrolizumab_page")["studies"]
    labeler, llm = scripted_labeler(tmp_path)
    plan = QueryPlan.model_validate({**DRUGS.model_dump(), "filters": {"drug": "pembrolizumab"}})
    pipeline = Pipeline(make_client(FakeCtGov(studies)), labeler=labeler)
    response = await pipeline.run_plan(QueryRequest(query="which drugs are combined with pembrolizumab?", max_citations=2), plan)

    assert response.status == "ok" and response.visualization.type == "network_graph"
    data = response.visualization.data
    assert data.nodes and data.edges
    assert "Pembrolizumab" in {n.label for n in data.nodes} and "Placebo" not in {n.label for n in data.nodes}
    assert set(response.citations) == {n.id for n in data.nodes} | {e.id for e in data.edges}
    assert response.meta.pruning.edges_shown == len(data.edges)
    assert response.meta.pruning.message.startswith("Showing")
    assert response.meta.units == {"size": "trials", "weight": "trials"}
    aggregate_step = next(s.model_dump() for s in response.meta.trace if s.step == "aggregate")
    assert aggregate_step["labels"]["asked_model"] > 0 and aggregate_step["dropped"]
    assert len(llm.requests) == 1
    # every edge joins two nodes that exist, and no node is left without an edge
    ids = {n.id for n in data.nodes}
    assert all(e.source in ids and e.target in ids for e in data.edges)
    assert ids == {e.source for e in data.edges} | {e.target for e in data.edges}
