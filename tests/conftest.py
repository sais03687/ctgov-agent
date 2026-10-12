"""TASK: Gives shared help for the tests. Tests read saved API responses and do not use the network.
GRADING: Code Quality (correctness).
"""
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _throwaway_log_file(tmp_path, monkeypatch):
    """Gives each test its own log file, so that a test does not write the real `logs/last_run.txt`."""
    monkeypatch.setenv("RUN_LOG_PATH", str(tmp_path / "last_run.txt"))


@pytest.fixture(scope="session")
def load_fixture() -> Callable[[str], dict[str, Any]]:
    """Returns a function that loads `tests/fixtures/<name>.json`."""

    def _load(name: str) -> dict[str, Any]:
        return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))

    return _load
