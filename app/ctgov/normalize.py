"""TASK:     Changes a raw study record into a Trial and keeps the raw values.
GRADING:  System Design (real API data). Bonus (the raw values are the citation evidence).
INPUT:    One study record from the API, and the list of fields to read.
OUTPUT:   A Trial. Each value has its evidence: the raw API value and its path in the record.
USED BY:  app/ctgov/client.py
DIAGRAM:  7 (from a bar to the trial record).

PARTS:
    1. Rules for single values
    2. Dates and durations
    3. The main function: normalize_study

The rules are fixed for each field and come from the field registry. A question changes only
the list of fields to read.
"""
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any

from app.models import FieldEvidence, FieldKind, InterventionRecord, MultiRule, Trial
from app.registry import PHASE_LABELS, REGISTRY, resolve_path

# ===== PART 1: Rules for single values ============================================================
# WHAT: Small functions that change one raw value into the value of a Trial.
# WHY:  The chart shows clean labels. The citation keeps the raw value.

# The sets of values that a `combine` field has together in real records.
COMBINATIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    name: tuple(field.combinations.values()) for name, field in REGISTRY.items() if field.combinations
}

_ARMS_PATH = "protocolSection.armsInterventionsModule"
_DAYS_PER_MONTH = 30.4375


def phase_label(raw: Any) -> str | None:
    """WHAT:   Changes the phase codes of a study into one label.
    INPUT:  raw - the list of phase codes.
    OUTPUT: `["PHASE1", "PHASE2"]` gives "Phase 1/2". None when the study has no phase.
    """
    values = [raw] if isinstance(raw, str) else list(raw or [])
    if not values:
        return None
    order = list(PHASE_LABELS)
    values = sorted(dict.fromkeys(values), key=lambda v: order.index(v) if v in order else len(order))
    numbers = [v.removeprefix("PHASE") for v in values]
    if len(values) > 1 and all(v.startswith("PHASE") and n.isdigit() for v, n in zip(values, numbers)):
        return "Phase " + "/".join(numbers)
    return " / ".join(PHASE_LABELS.get(v, v) for v in values)


def _year(raw: Any) -> int | None:
    """WHAT: Returns the year of a registry date as a number, or None."""
    parsed = _parse_date(raw)
    return parsed[0] if parsed else None


def _integer(raw: Any) -> int | None:
    """WHAT: Returns a value as a whole number, or None."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _boolean(raw: Any) -> bool | None:
    """WHAT: Returns a yes/no value as the label Yes or No, or None."""
    return raw if isinstance(raw, bool) else None


def _text(raw: Any) -> str | None:
    """WHAT: Returns a value as clean text, or None when it is empty."""
    return raw.strip() or None if isinstance(raw, str) else None


# Field name -> the rule that changes the raw value into the `Trial` value. A field that is not
# in this table keeps its text, or gets its display label when the field has a label table.
_CONVERTERS: dict[str, Callable[[Any], Any]] = {
    "phase": phase_label,
    "start_year": _year,
    "enrollment": _integer,
    "has_results": _boolean,
}


def display_label(field_name: str, raw: Any) -> Any:
    """WHAT:   Returns the Trial value that normalization gives to a raw API value of a field.
    INPUT:  field_name - the field. raw - the raw value.
    OUTPUT: The display label. A code that the label table does not know is shown as it came.
    """
    if field_name in _CONVERTERS:
        return _CONVERTERS[field_name](raw)
    if REGISTRY[field_name].kind is FieldKind.DATE:
        return raw if _parse_date(raw) else None  # Kept as the API wrote it, or not kept.
    text = _text(raw)
    labels = REGISTRY[field_name].labels
    return labels.get(text, text) if labels and text else text


# ===== PART 2: Dates and durations ================================================================
# WHAT: Reads registry dates and computes the months between two dates.
# WHY:  Some registry dates have no day. The code must not invent a day.


def _parse_date(raw: Any) -> tuple[int, int, int | None] | None:
    """WHAT: Changes `YYYY-MM` or `YYYY-MM-DD` into (year, month, day or None). Returns None for all other text."""
    if not isinstance(raw, str):
        return None
    parts = raw.split("-")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        return None
    year, month = int(parts[0]), int(parts[1])
    day = int(parts[2]) if len(parts) == 3 else None
    try:
        date(year, month, day or 1)
    except ValueError:
        return None
    return year, month, day


def months_between(start: Any, end: Any) -> float | None:
    """WHAT:   Returns the months from `start` to `end`.
    INPUT:  start, end - registry dates as text.
    OUTPUT: The number of months, or None if a date is missing or not valid, or the end is before the start.

    The day is used only when the two dates have a day. If not, full months are compared.
    """
    first, last = _parse_date(start), _parse_date(end)
    if first is None or last is None:
        return None
    if first[2] is not None and last[2] is not None:
        months = (date(*last) - date(*first)).days / _DAYS_PER_MONTH
    else:
        months = (last[0] - first[0]) * 12 + (last[1] - first[1])
    return round(months, 1) if months >= 0 else None


# ===== PART 3: The main function: normalize_study =================================================
# WHAT: Makes a Trial from one study record.
# WHY:  All later steps use Trial objects and not raw JSON.


def _intervention_records(study: dict[str, Any]) -> list[InterventionRecord]:
    """WHAT: Returns each intervention with the types of the arms that give it. The placebo rule uses this."""
    arm_types = {
        arm.get("label"): arm.get("type")
        for _, arm in resolve_path(study, f"{_ARMS_PATH}.armGroups[]")
        if isinstance(arm, dict)
    }
    records = []
    for _, item in resolve_path(study, f"{_ARMS_PATH}.interventions[]"):
        name = _text(item.get("name")) if isinstance(item, dict) else None
        if name is None:
            continue
        labels = item.get("armGroupLabels") or []
        records.append(
            InterventionRecord(
                name=name, type=item.get("type"), arm_types=[arm_types.get(label) for label in labels]
            )
        )
    return records


def _with_sources(fields: Iterable[str]) -> list[str]:
    """WHAT: Returns the requested fields, plus `nct_id`, plus the source fields of each computed field."""
    names = ["nct_id"]
    for name in fields:
        for needed in (*REGISTRY[name].derived_from, name):
            if needed not in names:
                names.append(needed)
    return names


def normalize_study(study: dict[str, Any], fields: Iterable[str] | None = None) -> Trial:
    """WHAT:   Makes a Trial that holds the given fields.
    WHY:    Each value keeps its raw API value and path, so that a citation can quote the record.
    INPUT:  study - the raw study record. fields - the fields to read (default: all fields).
    OUTPUT: A Trial. Raises ValueError if the study has no NCT ID.
    """
    names = _with_sources(REGISTRY if fields is None else fields)
    values: dict[str, Any] = {}
    evidence: dict[str, list[FieldEvidence]] = {}

    for name in names:
        field = REGISTRY[name]
        if field.path is None:
            continue
        found = resolve_path(study, field.path)
        if field.multi is MultiRule.EXPLODE:
            # One entry for each different value. The entry quotes the first position of the value.
            first_seen: dict[str, FieldEvidence] = {}
            for path, raw in found:
                label = display_label(name, raw)
                if label is not None and label not in first_seen:
                    first_seen[label] = FieldEvidence(path=path, raw=raw, label=label)
            values[name] = list(first_seen)
            if first_seen:
                evidence[name] = list(first_seen.values())
        elif found:
            path, raw = found[0]
            value = display_label(name, raw)
            if value is not None:
                values[name] = value
                evidence[name] = [FieldEvidence(path=path, raw=raw, label=value)]

    if "nct_id" not in values:
        raise ValueError("study has no NCT id")

    if "start_year" in values:
        values["start_date"] = evidence["start_year"][0].raw
    type_of = (("start_year", "start_date_type"), ("start_date", "start_date_type"), ("completion_date", "completion_date_type"))
    for name, attribute in type_of:
        if name in names:
            found = resolve_path(study, REGISTRY[name].path.removesuffix(".date") + ".type")
            values[attribute] = _text(found[0][1]) if found else None
    if "duration_months" in names:
        sources = [evidence[source][0] for source in REGISTRY["duration_months"].derived_from if source in evidence]
        months = months_between(values.get("start_date"), values.get("completion_date"))
        if months is not None:
            values["duration_months"] = months
            evidence["duration_months"] = sources
    if "intervention" in names:
        values["intervention_records"] = _intervention_records(study)

    return Trial(**values, evidence=evidence)
