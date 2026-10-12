"""TASK:     Puts a trial into buckets for one field. All plan modes use these rules.
GRADING:  Coverage. One set of rules for all fields, with no special cases.
INPUT:    A field name from the plan, and a Trial.
OUTPUT:   The bucket keys of the trial, the label of each bucket, and the sort order.
USED BY:  app/aggregate/aggregate.py, app/aggregate/histogram.py

PARTS:
    1. Bins and year ranges
    2. The Dimension class

The rules use only the description of the field in the field registry: its kind, its rule for
two or more values, and its spelling rule. No rule names a specific field.
"""
import math
from collections import Counter
from collections.abc import Hashable, Iterable

from app.models import NO_LABEL, UNKNOWN_LABEL, YES_LABEL, BinSpec, FieldDef, FieldKind, MultiRule, Scalar, Trial
from app.registry import match_key


# ===== PART 1: Bins and year ranges ===============================================================
# WHAT: Small functions for number bins and for a full range of years.
# WHY:  A chart must show empty years and empty bins as zero.


def bin_start(value: float, width: float) -> float:
    """WHAT: Returns the lower limit of the equal-width bin that holds `value`. A bin is [start, start + width)."""
    return math.floor(value / width) * width


def year_range(years: Iterable[int]) -> list[int]:
    """WHAT: Returns each year from the first to the last. Thus a year with no trials shows as zero."""
    years = list(years)
    return list(range(min(years), max(years) + 1)) if years else []


# ===== PART 2: The Dimension class ================================================================
# WHAT: One group-by field: trial -> bucket keys, key -> label, key -> sort position.
# WHY:  The same class works for all fields, so there is no code for a specific field.


class Dimension:
    """WHAT: One group-by field. It tells the buckets of a trial, the label of a bucket, and the order of the buckets."""

    def __init__(self, field: FieldDef, bin: BinSpec | None = None) -> None:
        """WHAT: Reads the rules of the field from the field registry. `bin` is the bin of a number field (optional)."""
        self.field = field
        self._width = bin.bin_width if bin and bin.field == field.name else None
        self._spellings: dict[Hashable, Counter[str]] = {}  # Merged name -> the original texts that were found.
        self.multi_valued = False  # True when a trial was in two or more buckets.

    @property
    def merged_spellings(self) -> bool:
        """WHAT: True when a bucket holds a name that was written in two or more ways."""
        return any(len(texts) > 1 for texts in self._spellings.values())

    def key_for(self, value: Hashable) -> Hashable:
        """WHAT: Returns the bucket key for one value: the value, or its spelling key when spellings are merged."""
        if self.field.merge_spellings is None:
            return value
        key = match_key(self.field.name, value)
        self._spellings.setdefault(key, Counter())[value] += 1
        return key

    def keys(self, trial: Trial) -> list[Hashable] | None:
        """WHAT:   Returns the buckets that contain the trial.
        INPUT:  trial - the trial.
        OUTPUT: A list of bucket keys, or None when the trial has no usable value.

        A missing category value goes into the "Unknown" bucket. A missing year or number has no
        correct position on an axis. Thus the trial is removed, and the caller counts it.
        """
        value = getattr(trial, self.field.name)
        kind = self.field.kind
        if self.field.multi is MultiRule.EXPLODE:
            keys = list(dict.fromkeys(self.key_for(item) for item in value))  # Two spellings of one name: count the trial one time.
            self.multi_valued = self.multi_valued or len(keys) > 1
            return keys or [UNKNOWN_LABEL]
        if kind is FieldKind.TIME:
            return None if value is None else [value]
        if kind is FieldKind.NUMERIC:
            if value is None or self._width is None:
                return None
            return [bin_start(value, self._width)]
        if value is None:
            return [UNKNOWN_LABEL]
        if kind is FieldKind.BOOLEAN:
            return [YES_LABEL if value else NO_LABEL]
        return [self.key_for(value)]

    def label(self, key: Hashable) -> Scalar:
        """WHAT: Returns the label of the bucket in the data rows."""
        if key in self._spellings:
            return self._spellings[key].most_common(1)[0][0]  # The most common original spelling.
        if self.field.kind is FieldKind.NUMERIC and self._width is not None:
            return f"{key:g}–{key + self._width:g}"
        return key

    def natural_position(self, key: Hashable) -> tuple:
        """WHAT: Returns the sort key for the natural order of the field.
        Years and bins go up. Phases go from early to late. All other values are alphabetical.
        "Unknown" is always last.
        """
        if key == UNKNOWN_LABEL:
            return (2, 0, "")
        if self.field.kind in (FieldKind.TIME, FieldKind.NUMERIC):
            return (0, key, "")
        if self.field.order and key in self.field.order:
            return (0, self.field.order.index(key), "")
        return (1, 0, str(self.label(key)).lower())
