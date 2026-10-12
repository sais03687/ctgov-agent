"""TASK:     Asks a small LLM to label drug names that the rules cannot decide.
GRADING:  AI / Agent Design. The LLM is the last step, and code examines its answer.
INPUT:    A list of intervention names.
OUTPUT:   A LabelOutcome: name -> label, plus the names that were refused.
USED BY:  app/aggregate/network.py (layer 4)
DIAGRAM:  6 (how a network removes items that are not drugs).

PARTS:
    1. The prompt and the output schema
    2. The result type
    3. The NameLabeler class

Limits on this LLM step:
    - One call for each request, only for names that the other layers did not decide.
    - The answer must use a fixed list of labels (strict JSON Schema).
    - Each returned name must be a name that was sent. The code refuses all other names.
    - A name that the model does not return stays in the chart.
    - Decided names are saved in a JSON file. The code asks about each name one time only.
    - If the model is not available, the network comes from the other three layers.
"""
import json
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.models import InterventionLabel
from app.registry import name_key

# ===== PART 1: The prompt and the output schema ===================================================
# WHAT: The instructions for the label model and the structure of its answer.
# WHY:  A strict schema permits only the four fixed labels.

LABEL_PROMPT = """You label intervention names taken from clinical trial records.

For each name, say what the name IS (not what role it plays in a trial):
- specific_drug: a named drug, biological, vaccine, antibody, cell or gene therapy, or a named
  combination of them. Code names (MK-3475, AZD9291) and brand names count. A named drug used as
  supportive care (dexamethasone, ondansetron) is still a specific_drug.
- placebo_or_sham: a placebo, sham, vehicle, saline control or dummy treatment.
- unspecific_care: care that names no particular drug: "chemotherapy", "standard of care",
  "best supportive care", "investigator's choice", "usual care", "standard treatment".
- not_a_drug: a procedure, surgery, radiation, device, diagnostic test, imaging, behavioural or
  dietary programme, questionnaire, sample collection or observation.

Return one entry per name, copying each name exactly as given. Do not add names."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "label": {"type": "string", "enum": [label.value for label in InterventionLabel]},
                },
                "required": ["name", "label"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["labels"],
    "additionalProperties": False,
}


# ===== PART 2: The result type ====================================================================
# WHAT: The labels found, and the answers that were refused.
# WHY:  The network must know which names to remove.


class LabelOutcome(BaseModel):
    """The result for a batch of names, after a look in the cache and a maximum of one call to the model."""

    labels: dict[str, InterventionLabel] = Field(default_factory=dict, description="Name as sent -> label.")
    from_cache: int = 0
    asked: int = Field(default=0, description="Names sent to the model in this call.")
    rejected: list[str] = Field(default_factory=list, description="Returned names that were never sent.")
    error: str | None = Field(default=None, description="Why the model could not be used, if so.")
    sent: list[str] = Field(default_factory=list, description="The names sent to the model.")
    raw: str | None = Field(default=None, description="The model's output text, word for word.")


# ===== PART 3: The NameLabeler class ==============================================================
# WHAT: Gets labels from the cache or from one LLM call, and examines the answer.
# WHY:  Code must not trust a name that the LLM invents.


class NameLabeler:
    """WHAT: Labels intervention names with a cache and a small LLM.
    `llm` is an `openai.AsyncOpenAI`. A test gives a fake object with the same `responses.create`.
    """

    def __init__(self, llm: Any = None, settings: Settings | None = None, cache_path: Path | None = None) -> None:
        """WHAT: Keeps the LLM client and the settings, and loads the cache file."""
        self._settings = settings or get_settings()
        self._llm = llm
        self._path = cache_path or self._settings.label_cache_path
        self._cache = self._load()

    def _load(self) -> dict[str, str]:
        """WHAT: Returns the labels in the cache file.
        WHY:  If the file is missing or damaged, the cache starts empty and the service still starts.
        """
        allowed = {label.value for label in InterventionLabel}
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(stored, dict):
            return {}
        return {key: value for key, value in stored.items() if isinstance(key, str) and value in allowed}

    async def label(self, names: list[str]) -> LabelOutcome:
        """WHAT:   Returns labels for the names.
        INPUT:  names - the intervention names.
        OUTPUT: A LabelOutcome. Known names come from the cache. The other names go to the model in
                one call. A returned name that was not sent is refused.
        """
        outcome = LabelOutcome()
        unknown: dict[str, str] = {}  # Cache key -> the name as it was sent.
        for name in dict.fromkeys(names):
            key = name_key(name)
            if key in self._cache:
                outcome.labels[name] = InterventionLabel(self._cache[key])
                outcome.from_cache += 1
            else:
                unknown.setdefault(key, name)
        if not unknown:
            return outcome

        outcome.asked = len(unknown)
        outcome.sent = list(unknown.values())
        try:
            outcome.raw = await self._ask(outcome.sent)
            returned = json.loads(outcome.raw)["labels"]
        except (openai.OpenAIError, ValueError, KeyError, TypeError) as exc:
            outcome.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return outcome

        learned = False
        for item in returned:
            key = name_key(str(item.get("name", "")))
            try:
                label = InterventionLabel(item.get("label"))
            except ValueError:
                label = None
            if key not in unknown or label is None:
                outcome.rejected.append(str(item.get("name")))  # The name was not sent, or the label is not known: do not use the answer.
                continue
            outcome.labels[unknown[key]] = label
            self._cache[key] = label.value
            learned = True
        if learned:
            self._save()
        return outcome

    async def _ask(self, names: list[str]) -> str:
        """WHAT: Does the one LLM call. Returns the exact output text of the model."""
        response = await self._client().responses.create(
            model=self._settings.label_model,
            instructions=LABEL_PROMPT,
            input=[{"role": "user", "content": json.dumps({"names": names}, ensure_ascii=False)}],
            text={"format": {"type": "json_schema", "name": "InterventionLabels", "schema": _SCHEMA, "strict": True}},
            reasoning={"effort": self._settings.label_reasoning_effort},
            store=False,
        )
        return response.output_text or ""

    def _client(self) -> Any:
        """WHAT: Returns the LLM client. Makes it on the first call."""
        if self._llm is None:
            key = self._settings.openai_api_key
            if key is None or not key.get_secret_value():
                raise ValueError("OPENAI_API_KEY is not set")
            self._llm = openai.AsyncOpenAI(api_key=key.get_secret_value(), timeout=self._settings.llm_timeout_seconds)
        return self._llm

    def _save(self) -> None:
        """WHAT: Writes the cache. If the disk refuses the write, the labels stay in memory for this process."""
        text = json.dumps(dict(sorted(self._cache.items())), ensure_ascii=False, indent=1)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass
