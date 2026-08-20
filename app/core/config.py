"""Runtime configuration.

Everything the system needs to run comes from the environment, with defaults
chosen so that a fresh clone works offline and without an API key. The offline
model adapter is the default on purpose: the test suite must pass with nothing
configured, and a reviewer should never have to spend money to check a claim.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Model
    # Only what `build_adapter` can actually construct. "openai" used to be
    # accepted here with no adapter behind it, so a valid-looking configuration
    # booted cleanly and then raised at the first model call. Advertising a
    # provider that does not exist is the same class of dishonesty as a success
    # message that is not true.
    llm_provider: Literal["offline", "anthropic"] = "offline"
    llm_model: str = "claude-sonnet-4-20250514"
    anthropic_api_key: str | None = None

    # How much of a document reaches the model. Real limits, stated, and
    # reported when they bite — see app/core/llm.py.
    classify_char_limit: int = 6_000
    extract_char_limit: int = 20_000
    # Largest single upload accepted, and the largest batch. Unbounded reads
    # were an unauthenticated way to exhaust memory.
    max_upload_bytes: int = 10 * 1024 * 1024
    max_upload_files: int = 50

    # Database
    database_url: str = "postgresql://doctask:doctask@localhost:5432/doctask"

    # Runtime
    log_level: str = "INFO"

    # A run that loops forever costs money and proves nothing. Bound it.
    max_model_calls_per_run: int = 200
    # How many times a stage may retry itself before escalating to a human.
    # Bounded retry is a decision the graph makes; unbounded retry is a bug.
    max_stage_retries: int = 2

    @property
    def uses_live_model(self) -> bool:
        return self.llm_provider != "offline"


@lru_cache
def get_settings() -> Settings:
    return Settings()
