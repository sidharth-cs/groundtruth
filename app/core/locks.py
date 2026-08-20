"""Serialising concurrent writers.

Two things in this system can be hit twice at once, and they need different
keys.

**A pile**, when two runs start against it together. Two runs against different
piles are genuinely independent — separate checkpoint threads, separate state,
nothing shared — so they are left alone to run in parallel.

**A case**, when two reviewers settle decisions on it together. This one was
missed for a long time and it is the more damaging of the two. `settle` reads
the whole decisions list, changes one entry and writes the whole list back,
under a reducer where the last writer wins outright. Two reviewers settling
different items at the same moment both receive a success, and one of the two
decisions is discarded. It was reproduced against the running API on the first
attempt; `tests/test_concurrent_review.py` keeps it reproduced.

Keying review on the *case* rather than the pile is deliberate. Two analysts
working two different cases that happen to share a pile have nothing to
contend over, and making them queue behind each other would be a lock that
teaches people to distrust the tool.

A Postgres advisory lock handles both. It is held by the session rather than a
transaction, it disappears if the process dies (so a SIGKILL mid-run cannot
wedge anything forever), and it needs no table.

The wait is bounded on purpose. Blocking indefinitely turns contention into a
hung process with no explanation; failing instantly turns a half-second overlap
into a spurious error. So it retries for a short window and then says plainly
what is happening and what is busy.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

import psycopg

from app.core.config import get_settings


class PileBusy(RuntimeError):
    """Another writer holds the lock for this resource."""


def _key(resource: str) -> int:
    """Stable 63-bit key for pg_advisory_lock.

    Python's hash() is salted per process and would give two processes
    different keys for the same resource, which is precisely the case this lock
    exists to catch.
    """
    import hashlib

    digest = hashlib.sha256(resource.encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


@contextmanager
def case_lock(
    thread: str, wait_seconds: float = 10.0, poll_seconds: float = 0.05
) -> Iterator[None]:
    """Hold an exclusive lock on one case for the duration of the block.

    Wrap every read-modify-write of a case's state: settling a decision,
    resuming past the gate, proposing an arrival. Without it two reviewers
    silently overwrite each other and both are told they succeeded.
    """
    with _advisory(f"case:{thread}", wait_seconds, poll_seconds,
                   f"another reviewer is settling this case ({thread})"):
        yield


@contextmanager
def pile_lock(
    pile_id: str, wait_seconds: float = 10.0, poll_seconds: float = 0.1
) -> Iterator[None]:
    """Hold an exclusive lock on one pile for the duration of the block."""
    with _advisory(
        f"pile:{pile_id}", wait_seconds, poll_seconds,
        f"another run is already working on pile {pile_id!r}",
    ):
        yield


@contextmanager
def _advisory(
    resource: str, wait_seconds: float, poll_seconds: float, busy_message: str
) -> Iterator[None]:
    key = _key(resource)
    conn = psycopg.connect(get_settings().database_url, autocommit=True)
    deadline = time.monotonic() + wait_seconds
    acquired = False
    try:
        while True:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                acquired = bool(cur.fetchone()[0])
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise PileBusy(
                    f"{busy_message}; waited {wait_seconds:g}s. Concurrent "
                    "writers are serialised so they cannot overwrite each "
                    "other's state. Independent work proceeds in parallel."
                )
            time.sleep(poll_seconds)
        yield
    finally:
        if acquired:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
        conn.close()


def is_locked(pile_id: str) -> bool:
    """Whether some session currently holds this pile. For tests and reporting."""
    key = _key(f"pile:{pile_id}")
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND objid = %s::bigint % 2147483648",
                (key,),
            )
            return cur.fetchone()[0] > 0
