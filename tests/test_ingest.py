"""A malformed document must fail itself, not the run.

Reproduced by the audit before the fix: a file with a `.pdf` or `.docx`
extension whose bytes are not actually that format raises out of the parser
(`pypdf.errors.PdfStreamError`, `docx.opc.exceptions.PackageNotFoundError`)
uncaught, which killed the whole intake stage with a bare HTTP 500 that never
named the offending file, and left the case permanently unusable because a
retry hit the same file again. The fix mirrors the empty-file path that
already existed: mark the one document FAILED and continue with the rest of
the pile, this time naming the reason.

No database needed — `read_document` and `load_pile` are pure filesystem and
parsing code.
"""

from __future__ import annotations

from pathlib import Path

from app.domain.models import DocumentStatus
from app.pipeline.ingest import load_pile, read_document


def test_a_malformed_pdf_fails_itself_not_the_run(tmp_path: Path):
    bad = tmp_path / "not_really_a.pdf"
    bad.write_bytes(b"this is not a pdf, just garbage bytes pretending to be one")

    document = read_document(bad, pile_id="p")

    assert document.status is DocumentStatus.FAILED
    assert document.text == ""
    assert document.quarantine_reason
    assert "not_really_a.pdf" in document.quarantine_reason


def test_a_malformed_docx_fails_itself_not_the_run(tmp_path: Path):
    bad = tmp_path / "not_really_a.docx"
    bad.write_bytes(b"this is not a docx, just garbage bytes pretending to be one")

    document = read_document(bad, pile_id="p")

    assert document.status is DocumentStatus.FAILED
    assert document.text == ""
    assert document.quarantine_reason
    assert "not_really_a.docx" in document.quarantine_reason


def test_load_pile_continues_past_a_malformed_document(tmp_path: Path):
    (tmp_path / "good.txt").write_text("Registered name: Northwind Traders Ltd.")
    (tmp_path / "bad.pdf").write_bytes(b"garbage, not a pdf")

    loaded = load_pile(tmp_path, pile_id="p")

    assert len(loaded) == 2
    statuses = {doc.filename: doc.status for doc, _ in loaded}
    assert statuses["good.txt"] is DocumentStatus.PARSED
    assert statuses["bad.pdf"] is DocumentStatus.FAILED


def test_a_genuinely_empty_file_still_fails_with_no_invented_reason(tmp_path: Path):
    """Unchanged behaviour: empty-but-valid input is not a parser error."""
    empty = tmp_path / "empty.txt"
    empty.write_text("")

    document = read_document(empty, pile_id="p")

    assert document.status is DocumentStatus.FAILED
    assert document.quarantine_reason is None
