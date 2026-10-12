"""TASK:     Reads the settings from the environment.
GRADING:  Code Quality. Settings are in one place only.
INPUT:    The `.env` file and the environment variables.
OUTPUT:   A Settings object.
USED BY:  All files that need a setting.

PARTS:
    1. Values from the .env file
    2. Fixed operation limits

This is the only file that reads the process environment. Thus a test can give different
settings without a change to the environment.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """WHAT: All settings of the service: the values from `.env`, and the fixed operation limits."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ===== PART 1: Values from the .env file =====
    # WHAT: The settings that a user can change.
    openai_api_key: SecretStr | None = None
    planner_model: str = "gpt-5.4-mini"
    label_model: str = "gpt-5.4-nano"
    # The specification said 10,000. A real question (sponsors and drugs for lung cancer,
    # 14,613 trials) was refused, so the limit increased. At 20,000, that question takes 28 s.
    max_trials: int = Field(default=20_000, ge=1)
    # The client uses a saved response until ClinicalTrials.gov publishes a new data timestamp
    # (approximately one time each day). This value is the life of a response when the timestamp
    # cannot be read. 0 stops the cache.
    cache_ttl_seconds: int = Field(default=3600, ge=0)

    # ===== PART 2: Fixed operation limits =====
    # WHAT: Limits for the registry client and the planner. Tests can make them smaller.
    # WHY:  The values come from measurements on the live API.
    ctgov_base_url: str = "https://clinicaltrials.gov/api/v2"
    ctgov_page_size: int = Field(default=1000, ge=1, le=1000)  # The API gives a maximum of 1000 and does not report it.
    ctgov_concurrency: int = Field(default=4, ge=1)
    ctgov_max_attempts: int = Field(default=3, ge=1)
    ctgov_backoff_seconds: float = Field(default=0.5, ge=0)  # HTTP 5xx or timeout: wait 0.5 s, then 1 s.

    # Rate limit. ClinicalTrials.gov does not document one. Measurements on 2026-10-11 showed:
    # request 11 in approximately 10 s gets HTTP 429, the block continues for 8 to 16 s, and there
    # is no Retry-After header. With 8 requests in 10 s, 40 requests ran with no HTTP 429.
    ctgov_rate_limit_requests: int = Field(default=8, ge=1)
    ctgov_rate_limit_window_seconds: float = Field(default=10.0, ge=0)
    ctgov_rate_limit_backoff_seconds: float = Field(default=4.0, ge=0)  # HTTP 429: wait 4 s, then 16 s.
    ctgov_max_retry_after_seconds: float = Field(default=60.0, ge=0)
    ctgov_timeout_seconds: float = Field(default=30.0, gt=0)
    ctgov_version_check_seconds: float = Field(default=600.0, ge=0)  # The time between two reads of the data timestamp.
    planner_max_tool_calls: int = Field(default=3, ge=0)
    planner_reasoning_effort: str = "low"
    label_reasoning_effort: str = "none"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    tier2_samples_per_bucket: int = Field(default=10, ge=1)
    label_cache_path: Path = PROJECT_ROOT / "data" / "label_cache.json"
    # The text log of the last question. It is erased when the next question arrives. None: no file.
    run_log_path: Path | None = PROJECT_ROOT / "logs" / "last_run.txt"


@lru_cache
def get_settings() -> Settings:
    """WHAT: Returns the settings. Reads the environment one time only."""
    return Settings()
