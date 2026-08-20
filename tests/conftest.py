"""Shared fixtures.

The whole suite runs with LLM_PROVIDER=offline and no API key. That is not a
convenience — the brief requires a reviewer to be able to check every claim
without spending money, and a suite that needs a key is a suite most reviewers
will never run.

Postgres is genuinely required, because the claims under test are about
persistence: resuming after a kill and two runs not corrupting each other
cannot be demonstrated against an in-memory stand-in. Tests that need it skip
with a clear reason rather than failing confusingly when it is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Set before anything imports app.core.config, whose settings are cached.
os.environ.setdefault("LLM_PROVIDER", "offline")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://doctask:doctask@localhost:5432/doctask"
)
# Belt and braces: if a key is lying around in the environment, the offline
# provider still ignores it, but tests should not depend on that.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)


def _postgres_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=3):
            return True
    except Exception:
        return False


needs_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason=(
        "Postgres is not reachable at DATABASE_URL. Start it with "
        "`docker compose up -d db`, or run the whole stack with `make up`."
    ),
)


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def meridian(repo: Path) -> Path:
    """The pile with deliberate defects: a conflict, an open alert, an
    over-invoice, and a document that tries to give orders."""
    return repo / "corpora" / "meridian"


@pytest.fixture(scope="session")
def northwind(repo: Path) -> Path:
    """The control. Nothing is wrong with it and nothing should be reported."""
    return repo / "corpora" / "northwind"


@pytest.fixture(scope="session")
def arrivals(repo: Path) -> Path:
    return repo / "corpora" / "_arrivals"


@pytest.fixture(scope="session")
def checklist_path(repo: Path) -> Path:
    return repo / "checklists" / "vendor_onboarding_v1.yaml"


@pytest.fixture(scope="session")
def watchlist_path(repo: Path) -> Path:
    return repo / "watchlists" / "synthetic_consolidated.json"


def screen_claims(claims, watchlist_path: Path) -> list[dict]:
    """Screen a pile's extracted parties, the way the pipeline does.

    Tests that evaluate a checklist need real screening state, not a hardcoded
    empty list. Passing `[]` would assert that a clean pile is clean by
    assumption; running the actual OFAC extract asserts it by evidence.
    """
    from app.domain.screening import PartyType, load_watchlist, screen
    from app.pipeline.graph import _alert_to_dict

    def value_of(attribute: str) -> str | None:
        return next((c.value for c in claims
                     if c.attribute == attribute and c.value), None)

    watchlist, _ = load_watchlist(watchlist_path)
    alerts = []
    if name := value_of("registered_name"):
        alerts += screen(name, PartyType.ENTITY, watchlist, subject_role="vendor",
                         subject_country=value_of("jurisdiction"),
                         subject_incorporated=value_of("incorporation_date"))
    if ubo := value_of("ubo_name"):
        alerts += screen(ubo, PartyType.INDIVIDUAL, watchlist,
                         subject_role="ultimate beneficial owner")
    return [_alert_to_dict(a) for a in alerts]


@pytest.fixture
def adapter():
    from app.core.llm import OfflineAdapter

    return OfflineAdapter()


@pytest.fixture(autouse=True, scope="session")
def _ensure_corpora(repo: Path):
    """Generate the corpora if they are missing, so a fresh clone can just run
    pytest. Screening dates are relative to generation time, so regenerating is
    also how the control pile stays genuinely clean over time."""
    if not (repo / "corpora" / "meridian").is_dir():
        import subprocess
        import sys

        subprocess.run(
            [sys.executable, str(repo / "scripts" / "make_corpora.py")],
            cwd=repo, check=True, capture_output=True,
        )
    yield
