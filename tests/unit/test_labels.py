"""TASK: Tests the label LLM step: the checks on its answer and its cache.
GRADING: AI / Agent Design (constraints on the LLM).
"""
import json

import httpx
import openai
import pytest

from app.aggregate.labels import LABEL_PROMPT, NameLabeler
from app.config import PROJECT_ROOT, Settings
from app.models import InterventionLabel
from tests.fakes import FakeLLM, llm_text


def answer(**labels: str):
    return llm_text(json.dumps({"labels": [{"name": name, "label": label} for name, label in labels.items()]}))


def labeler(llm, tmp_path, cache: dict | None = None) -> NameLabeler:
    path = tmp_path / "label_cache.json"
    if cache is not None:
        path.write_text(json.dumps(cache), encoding="utf-8")
    return NameLabeler(llm=llm, settings=Settings(_env_file=None), cache_path=path)


async def test_names_are_labelled_in_one_batched_call(tmp_path):
    llm = FakeLLM(answer(Temozolomide="specific_drug", Chemotherapy="unspecific_care", Radiotherapy="not_a_drug"))
    outcome = await labeler(llm, tmp_path).label(["Temozolomide", "Chemotherapy", "Radiotherapy"])
    assert outcome.labels == {
        "Temozolomide": InterventionLabel.SPECIFIC_DRUG,
        "Chemotherapy": InterventionLabel.UNSPECIFIC_CARE,
        "Radiotherapy": InterventionLabel.NOT_A_DRUG,
    }
    assert (outcome.asked, outcome.from_cache, outcome.rejected, outcome.error) == (3, 0, [], None)

    [request] = llm.requests  # one call for the whole batch
    assert request["model"] == "gpt-5.4-nano" and request["instructions"] == LABEL_PROMPT
    assert request["reasoning"] == {"effort": "none"} and request["store"] is False
    assert json.loads(request["input"][0]["content"]) == {"names": ["Temozolomide", "Chemotherapy", "Radiotherapy"]}
    schema = request["text"]["format"]
    assert schema["strict"] is True
    assert schema["schema"]["properties"]["labels"]["items"]["properties"]["label"]["enum"] == [
        "specific_drug", "placebo_or_sham", "unspecific_care", "not_a_drug",
    ]


async def test_a_name_the_model_invents_is_rejected_and_never_applied_or_cached(tmp_path):
    """Acceptance test: the model returns a name that was not sent."""
    llm = FakeLLM(answer(Temozolomide="specific_drug", Aspirin="specific_drug"))
    names = labeler(llm, tmp_path)
    outcome = await names.label(["Temozolomide"])
    assert outcome.labels == {"Temozolomide": InterventionLabel.SPECIFIC_DRUG}
    assert outcome.rejected == ["Aspirin"]
    assert json.loads((tmp_path / "label_cache.json").read_text(encoding="utf-8")) == {"temozolomide": "specific_drug"}


async def test_a_label_outside_the_fixed_set_is_rejected(tmp_path):
    llm = FakeLLM(answer(Temozolomide="probably_fine", Lomustine="specific_drug"))
    outcome = await labeler(llm, tmp_path).label(["Temozolomide", "Lomustine"])
    assert outcome.labels == {"Lomustine": InterventionLabel.SPECIFIC_DRUG} and outcome.rejected == ["Temozolomide"]


async def test_a_name_the_model_skips_stays_undecided_and_is_asked_again_next_time(tmp_path):
    llm = FakeLLM(answer(Temozolomide="specific_drug"), answer(Lomustine="specific_drug"))
    names = labeler(llm, tmp_path)
    first = await names.label(["Temozolomide", "Lomustine"])
    assert "Lomustine" not in first.labels
    second = await names.label(["Temozolomide", "Lomustine"])
    assert (second.from_cache, second.asked) == (1, 1)
    assert json.loads(llm.requests[1]["input"][0]["content"]) == {"names": ["Lomustine"]}


async def test_labels_are_cached_on_disk_and_never_asked_twice(tmp_path):
    llm = FakeLLM(answer(Temozolomide="specific_drug", Placebo="placebo_or_sham"))
    await labeler(llm, tmp_path).label(["Temozolomide", "Placebo"])

    fresh = labeler(llm, tmp_path)  # a new process reading the same file
    outcome = await fresh.label(["Placebo", "Temozolomide"])
    assert outcome.labels == {"Placebo": InterventionLabel.PLACEBO_OR_SHAM, "Temozolomide": InterventionLabel.SPECIFIC_DRUG}
    assert (outcome.from_cache, outcome.asked) == (2, 0)
    assert len(llm.requests) == 1


async def test_cache_ignores_capital_letters_and_hyphens_like_the_bars_do(tmp_path):
    llm = FakeLLM(answer(**{"Non-Drug Thing": "not_a_drug"}))
    names = labeler(llm, tmp_path, cache={"o6 benzylguanine": "specific_drug"})
    outcome = await names.label(["O6-Benzylguanine", "o6-benzylguanine"])
    assert outcome.labels == {"O6-Benzylguanine": InterventionLabel.SPECIFIC_DRUG, "o6-benzylguanine": InterventionLabel.SPECIFIC_DRUG}
    assert llm.requests == []


async def test_nothing_to_ask_means_no_call(tmp_path):
    llm = FakeLLM(answer())
    assert (await labeler(llm, tmp_path).label([])).labels == {}
    assert llm.requests == []


def _rate_limited():
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.RateLimitError("quota", response=httpx.Response(429, request=request), body=None)


@pytest.mark.parametrize("failure", [_rate_limited(), llm_text("not json"), llm_text('{"something": "else"}')])
async def test_a_failing_or_garbled_model_is_reported_not_raised_and_cached_names_still_work(tmp_path, failure):
    names = labeler(FakeLLM(failure), tmp_path, cache={"temozolomide": "specific_drug"})
    outcome = await names.label(["Temozolomide", "Mystery compound"])
    assert outcome.labels == {"Temozolomide": InterventionLabel.SPECIFIC_DRUG}
    assert outcome.error is not None and outcome.asked == 1
    assert json.loads((tmp_path / "label_cache.json").read_text(encoding="utf-8")) == {"temozolomide": "specific_drug"}


async def test_missing_api_key_is_an_error_outcome(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    names = NameLabeler(settings=Settings(_env_file=None, openai_api_key=None), cache_path=tmp_path / "c.json")
    outcome = await names.label(["Temozolomide"])
    assert outcome.labels == {} and "OPENAI_API_KEY is not set" in outcome.error


def test_the_committed_cache_file_is_valid():
    """`data/label_cache.json` comes with saved labels. Each entry must be a known label."""
    path = PROJECT_ROOT / "data" / "label_cache.json"
    cache = json.loads(path.read_text(encoding="utf-8"))
    assert len(cache) >= 100
    allowed = {label.value for label in InterventionLabel}
    assert set(cache.values()) <= allowed
    assert all(key == key.casefold() and key.strip() for key in cache)
    assert cache.get("temozolomide") == "specific_drug"
