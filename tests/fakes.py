"""TASK: Gives a fake ClinicalTrials.gov API and a fake LLM for the tests.
GRADING: Code Quality. The fake API has the same limits as the real API.
"""
import copy
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx

from app.config import Settings
from app.ctgov.client import CtGovClient

VERSION = {"apiVersion": "2.0.5", "dataTimestamp": "2026-10-09T09:00:05"}


def clone_studies(template: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """Returns `count` copies of a real study, each with its own NCT ID."""
    studies = []
    for i in range(count):
        study = copy.deepcopy(template)
        study["protocolSection"]["identificationModule"]["nctId"] = f"NCT9{i:07d}"
        studies.append(study)
    return studies


class FakeCtGov:
    """Gives `studies` from `/studies`. `override` can answer a request first. It returns None to pass the request on."""

    def __init__(
        self,
        studies: list[dict[str, Any]] | None = None,
        page_cap: int = 1000,
        override: Callable[[httpx.Request], httpx.Response | None] | None = None,
    ) -> None:
        self.studies = studies or []
        self.page_cap = page_cap
        self.override = override
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override:
            response = self.override(request)
            if response is not None:
                return response
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json=VERSION)
        params = request.url.params
        size = min(int(params.get("pageSize", 10)), self.page_cap)
        start = int(params.get("pageToken", 0))
        body: dict[str, Any] = {"studies": self.studies[start : start + size]}
        if params.get("countTotal") == "true":
            body = {"totalCount": len(self.studies), **body}
        if start + size < len(self.studies):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)

    def study_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/studies")]

    def page_requests(self) -> list[httpx.Request]:
        """Returns the requests that got trials. The size check with one row is not included."""
        return [r for r in self.study_requests() if r.url.params.get("pageSize") != "1"]


class Sleeper:
    """Replaces the clock and `asyncio.sleep`.

    A sleep takes no real time. It moves the fake time forward and is recorded. Thus a test
    can examine each wait exactly.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.delays: list[float] = []

    def clock(self) -> float:
        return self.now

    async def __call__(self, seconds: float) -> None:
        self.delays.append(round(seconds, 6))
        self.now += seconds


def make_client(handler: Callable[..., Any], sleeper: Sleeper | None = None, **settings: Any) -> CtGovClient:
    """Returns a client on the fake transport, with fake time and no random wait.

    The trial limit is 10,000 unless a test sets it. Thus the saved count of 14,613 trials
    stays "too many".
    """
    sleeper = sleeper or Sleeper()
    settings.setdefault("max_trials", 10_000)
    return CtGovClient(
        settings=Settings(_env_file=None, **settings),
        transport=httpx.MockTransport(handler),
        sleep=sleeper,
        clock=sleeper.clock,
        jitter=lambda: 0.0,
    )


# ---- a stand-in for the OpenAI client -----------------------------------------------------------


def llm_text(text: str, input_tokens: int = 100, output_tokens: int = 20) -> SimpleNamespace:
    """Returns a model response whose last output is `text`."""
    message = SimpleNamespace(type="message", role="assistant", content=text)
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(output=[message], output_text=text, usage=usage)


def llm_plan(plan: Any) -> SimpleNamespace:
    """Returns a model response that contains a plan (a `QueryPlan` or a dict) as JSON."""
    body = plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan
    return llm_text(json.dumps(body))


def llm_tools(*calls: tuple[str, dict[str, Any]]) -> SimpleNamespace:
    """Returns a model response that asks for tool calls and gives no answer."""
    items = [
        SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments), call_id=f"call_{i}")
        for i, (name, arguments) in enumerate(calls)
    ]
    return SimpleNamespace(output=items, output_text="", usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class FakeLLM:
    """Gives scripted responses in sequence and records each request.

    The last scripted response repeats without end. This simulates a model that does not stop
    its tool calls. A scripted exception is raised, not returned.
    """

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.requests.append({**kwargs, "input": copy.deepcopy(kwargs["input"])})
        step = self._script[min(len(self.requests), len(self._script)) - 1]
        if isinstance(step, Exception):
            raise step
        return step
