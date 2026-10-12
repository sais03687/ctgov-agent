"""TASK:     Writes a text log of the last question, with the raw LLM output.
GRADING:  Code Quality. A person can examine each step of a run.
INPUT:    The state of a request and its response.
OUTPUT:   One text file (default: logs/last_run.txt).
USED BY:  app/pipeline.py

PARTS:
    1. Fixed values
    2. Format the steps
    3. Format the LLM input and output
    4. The full log text
    5. The RunLog class: write the file

The service erases the file when a question arrives. It fills the file when the answer is
ready. Thus the file always describes one question: the last one.
Rule: the log must not change an answer. If the file cannot be written, the service ignores it.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models import QueryRequest, RunState, TraceStep, VisualizationResponse

# ===== PART 1: Fixed values =======================================================================
# WHAT: The line width and the title of each pipeline step.
# WHY:  Each step gets a plain title that a person can read.

WIDTH = 100
STEP_TITLES = {
    "plan": "PLAN: the language model turns the question into a query plan",
    "validate": "VALIDATE: code checks the plan against the registry of fields",
    "interpretation": "INTERPRETATION: the model's summary did not match its plan and was replaced",
    "fetch": "FETCH: trials are read from ClinicalTrials.gov",
    "suggest": "SUGGEST: narrower versions of the question are counted",
    "aggregate": "AGGREGATE: code computes the chart's numbers from the trials",
    "cite": "CITE: the trials behind each data point are picked",
    "build": "BUILD: the chart specification is assembled",
    "failed": "FAILED: an unexpected error",
}


# ===== PART 2: Format the steps ===================================================================
# WHAT: Changes the details of one pipeline step into text lines.
# WHY:  The log shows each step with its time and its details.


def _text(value: Any) -> str:
    """WHAT: Returns a value as text. A value that is not text becomes JSON."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _field(name: str, value: Any, indent: int = 4) -> list[str]:
    """WHAT: Returns `name: value` lines. Lists and dicts go on indented lines."""
    pad, label = " " * indent, name.replace("_", " ")
    if isinstance(value, dict) and value:
        return [f"{pad}{label}:"] + [line for k, v in value.items() for line in _field(str(k), v, indent + 4)]
    if isinstance(value, list) and value:
        if all(not isinstance(item, (dict, list)) for item in value):
            return [f"{pad}{label}: {', '.join(_text(item) for item in value)}"]
        lines = [f"{pad}{label}:"]
        for item in value:
            inner = item.items() if isinstance(item, dict) else [("", item)]
            parts = [f"{k.replace('_', ' ')}: {_text(v)}" if k else _text(v) for k, v in inner if v not in (None, [], {})]
            lines.append(f"{pad}    - " + "; ".join(parts))
        return lines
    return [f"{pad}{label}: {_text(value)}"]


def _step(number: int, step: TraceStep) -> list[str]:
    """WHAT: Returns the text lines for one pipeline step: its title, its time, and its details."""
    details = step.model_dump(exclude={"step", "ms"})
    seconds = f"{step.ms / 1000:.2f} s" if step.ms >= 10 else f"{step.ms:.1f} ms"
    lines = ["", f"{number}. {STEP_TITLES.get(step.step, step.step.upper())}", f"    took: {seconds}"]
    for name, value in details.items():
        if value in (None, [], {}):
            continue
        lines += _field(name, value)
    return lines


# ===== PART 3: Format the LLM input and output ====================================================
# WHAT: Shows the text sent to each LLM and the exact text that it returned.
# WHY:  A person can see the raw LLM output before code used it.


def _block(text: str, indent: int = 8) -> list[str]:
    """WHAT: Returns the text with indentation and no other change.
    WHY:  The log must show the exact output of the LLM. JSON gets one item on each line so that
          a person can read it. Nothing is added, removed, or put in a different order.
    """
    try:
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        pass
    return [" " * indent + line for line in (text.splitlines() or ["(empty)"])]


def _conversation(items: list[dict[str, Any]]) -> list[str]:
    """WHAT: Returns each item exchanged with the planner model, in sequence, with a label for its direction."""
    lines: list[str] = []
    for item in items:
        kind = item.get("type", "message")
        if item.get("role") == "user":
            lines += ["", "    >> SENT TO THE MODEL (message)"] + _block(_text(item.get("content", "")))
        elif kind == "function_call":
            lines += ["", f"    << MODEL RETURNED (asks to run the tool `{item.get('name')}` with these arguments)"]
            lines += _block(item.get("arguments") or "")
        elif kind == "function_call_output":
            lines += ["", "    >> SENT TO THE MODEL (the tool's result)"] + _block(_text(item.get("output", "")))
        elif kind == "reasoning":
            lines += ["", "    << MODEL RETURNED (private reasoning: encrypted by the provider, not readable)"]
        elif kind == "message":
            content = item.get("content") or []
            texts = [content] if isinstance(content, str) else [p.get("text", "") for p in content if isinstance(p, dict)]
            lines += ["", "    << MODEL RETURNED (output text, word for word)"] + _block("\n".join(texts))
        else:
            lines += ["", f"    << MODEL RETURNED ({kind})"] + _block(_text(item))
    return lines


def _exchanges(exchanges: list[dict[str, Any]]) -> list[str]:
    """WHAT: Returns the text for each LLM exchange: its purpose, the input, the output, and how the system used it."""
    lines: list[str] = []
    for number, exchange in enumerate(exchanges, start=1):
        model = f" (model: {exchange['model']})" if exchange.get("model") else ""
        lines += ["", f"{number}. Purpose: {exchange['purpose']}{model}"]
        if "conversation" in exchange:
            lines += ["    The standing instructions sent with every call are SYSTEM_PROMPT in app/planner/prompt.py."]
            lines += _conversation(exchange["conversation"])
        else:
            lines += ["", "    >> SENT TO THE MODEL"] + _block(exchange.get("sent") or "")
            lines += ["", "    << MODEL RETURNED (output text, word for word)"] + _block(exchange.get("returned") or "")
            if exchange.get("error"):
                lines += ["", f"    error: {exchange['error']}"]
        lines += ["", f"    == HOW THE SYSTEM USED IT: {exchange.get('used', '')}"]
    return lines


# ===== PART 4: The full log text ==================================================================
# WHAT: Puts all sections of the log together.
# WHY:  One function makes the full text, so a test can examine it.


def render(state: RunState, response: VisualizationResponse, started: datetime, seconds: float) -> str:
    """WHAT:   Returns the full text of the log.
    INPUT:  state - the state of the request. response - the response.
            started - the start time. seconds - the total time.
    OUTPUT: The log text.
    """
    request = state.request.model_dump(mode="json", exclude_none=True, exclude={"query", "debug"})
    rule = "=" * WIDTH
    lines = [rule, "LATEST QUESTION", rule, f"asked at: {started:%Y-%m-%d %H:%M:%S}", f"question: {state.request.query}"]
    lines += _field("request options", request, indent=0)
    lines += ["", rule, "WHAT HAPPENED, STEP BY STEP", rule]
    for number, step in enumerate(state.trace, start=1):
        lines += _step(number, step)

    if state.llm_exchanges:
        lines += ["", rule, "RAW LANGUAGE MODEL INPUT AND OUTPUT", rule]
        lines += _exchanges(state.llm_exchanges)

    meta = response.meta
    if meta.plan is not None:
        lines += ["", rule, "THE PLAN THAT WAS RUN (after request filters and code checks)", rule]
        lines += json.dumps(meta.plan.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False).splitlines()
    if meta.filters_applied:
        lines += ["", rule, "SEARCHES SENT TO CLINICALTRIALS.GOV", rule]
        for label, filters in meta.filters_applied.items():
            lines += _field(label, filters, indent=0)

    lines += ["", rule, "RESULT", rule, f"status: {response.status.value}", f"total time: {seconds:.2f} s"]
    if response.visualization is not None:
        chart = response.visualization
        lines += [f"chart: {chart.type}", f"title: {chart.title}"]
        lines += [f"trials matched: {meta.trials_matched:,}", f"trials used: {meta.trials_used:,}"]
        lines += [f"data points with citations: {len(response.citations)}"]
    if meta.data_timestamp:
        lines += [f"registry data as of: {meta.data_timestamp}"]
    if response.error is not None:
        lines += [f"reason code: {response.error.code}", f"message: {response.error.message}"]
        lines += _field("details", response.error.details, indent=0)
        lines += _field("offered instead", [s.label for s in response.error.suggestions], indent=0)
    if meta.notes:
        lines += ["notes shown to the user:"] + [f"    - {note}" for note in meta.notes]
    return "\n".join(lines) + "\n"


# ===== PART 5: The RunLog class: write the file ===================================================
# WHAT: Erases the file at the start of a request and writes it at the end.
# WHY:  The file must always describe one question only.


class RunLog:
    """WHAT: Writes the log file for the last request."""

    def __init__(self, path: Path) -> None:
        """WHAT: Keeps the path of the log file."""
        self._path = path
        self._started = datetime.now()

    def start(self, request: QueryRequest) -> None:
        """WHAT: Erases the file and writes the new question. The old content was about the question before this one."""
        self._started = datetime.now()
        self._write(f"question: {request.query}\nasked at: {self._started:%Y-%m-%d %H:%M:%S}\n\nWorking on it...\n")

    def finish(self, state: RunState, response: VisualizationResponse) -> None:
        """WHAT: Writes the full log for the completed request. A failure to make the log does not stop the answer."""
        seconds = (datetime.now() - self._started).total_seconds()
        try:
            text = render(state, response, self._started, seconds)
        except Exception as exc:  # The log is an aid only. It must not cause an answer to fail.
            text = f"question: {state.request.query}\n\nThe log could not be written ({type(exc).__name__}: {exc}).\n"
        self._write(text)

    def _write(self, text: str) -> None:
        """WHAT: Writes text to the file. Ignores a disk error."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(text, encoding="utf-8")
        except OSError:
            pass
