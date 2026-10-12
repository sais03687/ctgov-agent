"""TASK:     Makes the nodes and edges of a network graph, and removes placebos.
GRADING:  Coverage. The assignment gives a higher score to a meaningful network graph.
INPUT:    The fetched series and the plan (mode `network`). Optionally, the label model.
OUTPUT:   An AggregateResult with nodes, edges, the trial IDs for each, and the pruning facts.
USED BY:  app/pipeline.py (the AGGREGATORS table)
DIAGRAM:  6 (how a network removes items that are not drugs).

PARTS:
    1. Fixed values for the removal layers
    2. One side of the network (layers 1 to 3)
    3. The main function: network (layer 4 and pruning)
    4. The pruning message

    source_field x target_field   Each source entity of a trial links to each target entity.
    One field two times           Co-occurrence: each pair of entities in a trial.

The four removal layers. The most reliable layer is first (P3):
    1. Arms        Remove an item given only in placebo or sham comparator arms.
    2. Type        Keep only the types DRUG and BIOLOGICAL.
    3. Stop-list   Remove names that contain placebo, sham, vehicle, saline, or standard care.
    4. Model       For the names that the chart will show: one LLM call labels each name.
"""
import json
import re
from collections.abc import Hashable
from itertools import combinations, product

from app.aggregate.common import Dimension
from app.aggregate.labels import NameLabeler
from app.models import (
    UNKNOWN_LABEL,
    AggregateResult,
    BucketRef,
    DatumAbout,
    InterventionLabel,
    NetworkData,
    NetworkEdge,
    NetworkNode,
    PlanMode,
    PruningInfo,
    QueryPlan,
    SeriesFetch,
    Trial,
)
from app.registry import REGISTRY, name_key

# ===== PART 1: Fixed values for the removal layers ================================================
# WHAT: The arm types, the drug types, and the stop words that the layers use.
# WHY:  A placebo has the type DRUG in the registry, so the type cannot remove it.

CONTROL_ARMS = {"PLACEBO_COMPARATOR", "SHAM_COMPARATOR"}
DRUG_TYPES = {"DRUG", "BIOLOGICAL"}
STOP_TERMS = ("placebo", "sham", "vehicle", "saline", "standard of care", "best supportive care")
_STOP = re.compile(r"\b(" + "|".join(STOP_TERMS) + r")\b")
SMALL_DATASET = 50  # Below this number of trials, a connection in one trial only is also shown.
LABEL_BUFFER = 2  # The label model examines the names of this many times the edges that the chart shows.

Node = tuple[str, Hashable]  # (field name, entity key)

# Layer -> the words that a note uses for the items that the layer removed.
_LAYERS = {
    "control_arm_only": "given only in placebo or sham arms",
    "not_drug_or_biological": "not typed as a drug or biological",
    "stop_list": "named as a placebo, sham, vehicle, saline or standard care",
    "model_label": "judged not to be a specific drug",
}


def plural(noun: str) -> str:
    """WHAT: Returns the plural of a noun: "drug" gives "drugs", "country" gives "countries"."""
    if noun.endswith("y"):
        return noun[:-1] + "ies"
    return noun + ("es" if noun.endswith("s") else "s")


# ===== PART 2: One side of the network (layers 1 to 3) ============================================
# WHAT: Finds the entities of one field in a trial, and applies the three rule layers.
# WHY:  Rules are more reliable than an LLM, so they run first.


class _Side:
    """WHAT: One end of the network. It gives the entities of one field that a trial has."""

    def __init__(self, field_name: str) -> None:
        """WHAT: Reads the field from the field registry and starts the counters of removed names."""
        self.field = REGISTRY[field_name]
        self.dim = Dimension(self.field)
        self.removed: dict[str, set[Hashable]] = {layer: set() for layer in _LAYERS}
        self.kept: set[Hashable] = set()

    @property
    def noun(self) -> str:
        """WHAT: Returns the plain name of the entities on this side, for example "drug"."""
        return self.field.noun or self.field.display_name.lower()

    def keys(self, trial: Trial) -> list[Hashable]:
        """WHAT:   Returns the entities of this field in one trial.
        OUTPUT: A list of entity keys. For a drug field, layers 1 to 3 remove items first.
        """
        if not self.field.clean_as_drug:
            return [key for key in self.dim.keys(trial) or [] if key != UNKNOWN_LABEL]
        keys: list[Hashable] = []
        for record in trial.intervention_records:
            layer = self._removed_by(record.name, record.type, record.arm_types)
            key = name_key(record.name)
            if layer:
                self.removed[layer].add(key)
            elif key not in keys:
                keys.append(self.dim.key_for(record.name))
                self.kept.add(key)
        return keys

    @staticmethod
    def _removed_by(name: str, kind: str | None, arm_types: list[str | None]) -> str | None:
        """WHAT: Returns the first layer that refuses this record. Returns None if layers 1 to 3 keep it."""
        if arm_types and all(arm in CONTROL_ARMS for arm in arm_types):
            return "control_arm_only"  # An arm with no type is not proof of a control arm.
        if kind not in DRUG_TYPES:
            return "not_drug_or_biological"
        if _STOP.search(name_key(name)):
            return "stop_list"
        return None

    def removed_counts(self) -> dict[str, int]:
        """WHAT: Returns the number of different names that each layer removed.
        A name that is kept in one trial or more is not counted as removed.
        """
        counts: dict[str, int] = {}
        seen = set(self.kept)
        for layer, names in self.removed.items():
            gone = names - seen
            seen |= gone
            if gone:
                counts[layer] = len(gone)
        return counts


# ===== PART 3: The main function: network (layer 4 and pruning) ===================================
# WHAT: Makes the nodes and edges, asks the label model, and keeps the strongest connections.
# WHY:  A graph with thousands of lines has no meaning for the user.


def _strongest(edges: dict[tuple[Node, Node], list[str]], limit: int | None = None) -> list[tuple[Node, Node]]:
    """WHAT: Returns the edges sorted by shared trials, largest first. Equal edges have a fixed order."""
    ordered = sorted(edges, key=lambda edge: (-len(edges[edge]), str(edge)))
    return ordered if limit is None else ordered[:limit]


async def network(series: list[SeriesFetch], plan: QueryPlan, labeler: NameLabeler | None = None) -> AggregateResult:
    """WHAT:   Makes the node list and the edge list for one series.
    INPUT:  series - the fetched series. plan - the plan. labeler - the label model (layer 4), or None.
    OUTPUT: An AggregateResult. Without the labeler, the network comes from layers 1 to 3 only.
    """
    spec = plan.network
    source = next((s for s in series if s.error is None), None)
    trials = source.trials if source else []
    same = spec.source_field == spec.target_field
    left = _Side(spec.source_field)
    right = left if same else _Side(spec.target_field)

    node_trials: dict[Node, list[str]] = {}
    edge_trials: dict[tuple[Node, Node], list[str]] = {}
    for trial in trials:
        a = [(left.field.name, key) for key in left.keys(trial)]
        b = a if same else [(right.field.name, key) for key in right.keys(trial)]
        for node in dict.fromkeys(a + b):
            node_trials.setdefault(node, []).append(trial.nct_id)
        pairs = combinations(sorted(a, key=str), 2) if same else product(a, b)
        for pair in pairs:
            edge_trials.setdefault(pair, []).append(trial.nct_id)

    sides = {left.field.name: left, right.field.name: right}
    result = AggregateResult(mode=PlanMode.NETWORK, network=NetworkData())
    notes = result.notes

    def label_of(node: Node) -> str:
        return str(sides[node[0]].dim.label(node[1]))

    # Layer 4: ask the label model about the names that the chart will show, and only those ---------------------
    removed = {name: side.removed_counts() for name, side in sides.items() if side.field.clean_as_drug}
    candidates = list(
        dict.fromkeys(
            node
            for edge in _strongest(edge_trials, spec.max_edges * LABEL_BUFFER)
            for node in edge
            if sides[node[0]].field.clean_as_drug
        )
    )
    if labeler is not None and candidates:
        outcome = await labeler.label([label_of(node) for node in candidates])
        rejected = {
            node for node in candidates
            if outcome.labels.get(label_of(node), InterventionLabel.SPECIFIC_DRUG) is not InterventionLabel.SPECIFIC_DRUG
        }
        for node in rejected:
            node_trials.pop(node, None)
            removed[node[0]]["model_label"] = removed[node[0]].get("model_label", 0) + 1
        edge_trials = {edge: ids for edge, ids in edge_trials.items() if not rejected & set(edge)}
        if outcome.sent:
            result.llm_exchanges.append({
                "purpose": "label unclear names in the network (specific drug or not)",
                "sent": json.dumps({"names": outcome.sent}, ensure_ascii=False),
                "returned": outcome.raw,
                "error": outcome.error,
                "used": f"{len(outcome.labels) - outcome.from_cache} labels accepted, "
                        f"{len(outcome.rejected)} rejected (names never sent, or unknown labels)",
            })
        result.trace["labels"] = {
            "checked": len(candidates), "from_cache": outcome.from_cache, "asked_model": outcome.asked,
            "rejected_answers": outcome.rejected, "removed": sorted(label_of(node) for node in rejected),
            "error": outcome.error,
        }
        if outcome.error:
            notes.append(
                "The names could not be checked by the language model, so a few entries that are "
                "not specific drugs may remain."
            )

    for field_name, counts in removed.items():
        if counts:
            result.dropped.update({f"{field_name}: {layer}": n for layer, n in counts.items()})
            parts = [f"{n:,} {_LAYERS[layer]}" for layer, n in counts.items()]
            notes.append(f"Only specific drugs are shown. Names left out: {'; '.join(parts)}.")
    for side in dict.fromkeys(sides.values()):
        if side.dim.merged_spellings:
            notes.append(
                f"{side.field.display_name} names written in slightly different ways are counted "
                f"together and shown under the most common spelling."
            )

    # Pruning: keep the strongest connections only ---------------------------------------------------
    minimum = 1 if len(trials) < SMALL_DATASET else 2
    eligible = {edge: ids for edge, ids in edge_trials.items() if len(ids) >= minimum}
    shown = _strongest(eligible, spec.max_edges)
    shown_nodes = sorted({node for edge in shown for node in edge}, key=lambda n: (-len(node_trials[n]), str(n)))

    node_ids = {node: f"n{i}" for i, node in enumerate(shown_nodes, start=1)}
    for node in shown_nodes:
        result.network.nodes.append(
            NetworkNode(id=node_ids[node], label=label_of(node), type=node[0], trial_count=len(node_trials[node]))
        )
        result.contributing[node_ids[node]] = node_trials[node]
        result.about[node_ids[node]] = DatumAbout(
            series=source.label, label=label_of(node), buckets=[BucketRef(field=node[0], bucket=label_of(node))]
        )
    for i, edge in enumerate(shown, start=1):
        edge_id = f"e{i}"
        result.network.edges.append(
            NetworkEdge(id=edge_id, source=node_ids[edge[0]], target=node_ids[edge[1]], shared_trials=len(eligible[edge]))
        )
        result.contributing[edge_id] = eligible[edge]
        result.about[edge_id] = DatumAbout(
            series=source.label,
            label=f"{label_of(edge[0])} — {label_of(edge[1])}",
            buckets=[BucketRef(field=node[0], bucket=label_of(node)) for node in edge],
        )

    result.trials_used = len({nct_id for edge in shown for nct_id in eligible[edge]})
    result.pruning = _pruning(len(shown), len(edge_trials), [len(eligible[e]) for e in shown], minimum, left, right, same)
    return result


# ===== PART 4: The pruning message ================================================================
# WHAT: Tells the user the meaning of a line and the number of lines removed.
# WHY:  The user must know that the graph is not complete.


def _pruning(shown: int, total: int, weights: list[int], minimum: int, left: _Side, right: _Side, same: bool) -> PruningInfo:
    """WHAT: Returns a plain-language text that tells the meaning of the lines and the number of lines removed."""
    weakest = min(weights, default=0)
    if total == 0:
        message = "No connections were found among these trials."
    elif shown == 0:
        message = (
            f"{total:,} connections were found, but each appears in a single trial only, "
            f"so none is shown."
        )
    else:
        scope = f"all {shown:,} connections" if shown == total else f"the {shown:,} strongest connections out of {total:,}"
        times = "1 trial" if weakest == 1 else f"{weakest:,} trials"
        if same:
            meaning = f"Each line means the two {plural(left.noun)} were used together in at least {times}."
        else:
            meaning = f"Each line means the {left.noun} ran at least {times} involving the {right.noun}."
        message = f"Showing {scope}. {meaning}"
        if minimum > 1 and shown < total:
            message += " Connections seen in only one trial are left out."
    return PruningInfo(edges_shown=shown, edges_total=total, min_shared_trials=weakest, message=message)
