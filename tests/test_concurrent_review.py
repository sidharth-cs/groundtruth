"""Two reviewers on one case must not overwrite each other.

The brief: *"Two runs at the same time stay two runs... Concurrent work does not
corrupt state."* Runs were already serialised by a pile lock. Review was not.

`settle` reads the whole decisions list, changes one entry and writes the whole
list back, under a `_keep_last` reducer where the last writer wins outright. Two
reviewers settling different items at the same moment both get `200`, and one of
the two decisions is discarded — a success message that is not true, in the
approval gate itself.

This was not assumed. It was reproduced against the running API on the first
attempt before any lock was written, and this test is that reproduction made
deterministic enough to keep.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from tests.conftest import needs_postgres


@pytest.fixture
def client():
    return TestClient(app)


def states_of(client, thread) -> dict[int, str]:
    body = client.get(f"/api/runs/{thread}").json()
    return {d["index"]: d["state"] for d in body["decisions"]}


@needs_postgres
@pytest.mark.parametrize("trial", range(3))
def test_two_reviewers_settling_at_once_both_survive(client, trial):
    """The race, made repeatable.

    Both settles are released from a barrier so their read-modify-write windows
    overlap. Either both succeed and both land, or one is refused — what must
    never happen is both returning 200 with only one decision recorded.
    """
    thread = f"race-{uuid.uuid4().hex[:8]}"
    started = client.post("/api/runs", json={"pile": "meridian", "thread": thread})
    assert started.status_code == 200
    assert len(started.json()["decisions"]) >= 3

    gate = threading.Barrier(2)
    codes: dict[int, int] = {}

    def settle(index: int) -> None:
        gate.wait()
        response = client.post(
            f"/api/runs/{thread}/decisions/{index}/reject",
            json={"note": f"reviewer-{index}"},
        )
        codes[index] = response.status_code

    workers = [threading.Thread(target=settle, args=(i,)) for i in (1, 2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    states = states_of(client, thread)
    for index, code in codes.items():
        if code == 200:
            assert states[index] == "rejected", (
                f"decision {index} returned 200 but is still {states[index]!r}. "
                f"A reviewer was told their decision was recorded and it was not. "
                f"(both codes: {codes})"
            )


@needs_postgres
def test_settling_the_same_item_twice_at_once_is_refused_once(client):
    """Two reviewers reaching for the same item is a different case from two
    reaching for different ones. Exactly one may win; the other must be told
    plainly rather than silently overwriting the first."""
    thread = f"race-same-{uuid.uuid4().hex[:8]}"
    client.post("/api/runs", json={"pile": "meridian", "thread": thread})

    gate = threading.Barrier(2)
    codes: list[int] = []
    lock = threading.Lock()

    def settle(note: str) -> None:
        gate.wait()
        response = client.post(
            f"/api/runs/{thread}/decisions/1/reject", json={"note": note}
        )
        with lock:
            codes.append(response.status_code)

    workers = [threading.Thread(target=settle, args=(n,)) for n in ("a", "b")]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert sorted(codes) == [200, 409], (
        f"expected one success and one refusal, got {codes}. Two writers both "
        "settling one item means one silently replaced the other."
    )
    assert states_of(client, thread)[1] == "rejected"
