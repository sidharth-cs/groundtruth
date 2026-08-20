"""Provenance: nothing is asserted that cannot be traced to a source.

These are the tests behind "it never bluffs". They check the invariants that
make the register trustworthy, not that any particular value was extracted.
"""

from __future__ import annotations

import pytest

from app.domain.models import (
    Chunk,
    Citation,
    Claim,
    Register,
    RegisterSection,
    SupportLevel,
)
from app.pipeline.ingest import load_pile, slice_for
from uuid import uuid4


def test_supported_claim_cannot_exist_without_a_citation():
    with pytest.raises(ValueError, match="traceable"):
        Claim(
            pile_id="p", attribute="registered_name", value="Acme Ltd",
            support=SupportLevel.SUPPORTED, citations=[],
        )


def test_unsupported_claim_cannot_carry_a_value():
    """The system must not invent a value it could not source."""
    with pytest.raises(ValueError, match="must not invent"):
        Claim(
            pile_id="p", attribute="ubo_name", value="A. Guess",
            support=SupportLevel.UNSUPPORTED,
        )


def test_unsupported_claim_with_no_value_is_the_honest_answer():
    claim = Claim(
        pile_id="p", attribute="ubo_name", value=None,
        support=SupportLevel.UNSUPPORTED,
    )
    assert claim.value is None


def test_chunk_rejects_offsets_that_do_not_match_its_text():
    """A drifting offset makes every citation into it unverifiable."""
    with pytest.raises(ValueError, match="unverifiable"):
        Chunk(document_id=uuid4(), ordinal=0, text="abc", char_start=0, char_end=99)


def test_citation_verifies_against_its_source():
    text = "Registered name: Acme Holdings Ltd"
    cit = Citation(
        document_id=uuid4(), chunk_id=uuid4(),
        char_start=17, char_end=34, quote=text[17:34],
    )
    assert cit.verify(text)
    assert not cit.verify("something else entirely")


@pytest.mark.parametrize("pile", ["meridian", "northwind"])
def test_every_chunk_reslices_byte_exact(repo, pile):
    """Chunk offsets index into the same normalised text citations are checked
    against. If this drifts, every citation in the register is worthless."""
    loaded = load_pile(repo / "corpora" / pile, pile)
    assert loaded, "pile produced no documents"
    for document, chunks in loaded:
        assert chunks, f"{document.filename} produced no chunks"
        for chunk in chunks:
            assert slice_for(document, chunk.char_start, chunk.char_end) == chunk.text


def test_register_section_hashes_isolate_change():
    """The basis of the 'nothing else changed' proof."""
    register = Register(pile_id="p", sections=[
        RegisterSection(id="a", heading="A", body="one"),
        RegisterSection(id="b", heading="B", body="two"),
    ])
    before = register.hashes()
    register.sections[1].body = "two (amended)"
    after = register.hashes()

    assert before["a"] == after["a"], "an untouched section changed hash"
    assert before["b"] != after["b"], "a changed section kept its hash"
