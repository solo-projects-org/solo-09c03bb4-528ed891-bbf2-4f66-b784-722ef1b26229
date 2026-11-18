"""Unit tests for revision-difference QC (pairing rules and de-identification)."""
from __future__ import annotations

from datetime import date

from app.csvutil import Row
from app.diffqc import (
    ADDED,
    AMBIGUOUS,
    CORRECTED,
    REMOVED,
    compute_diff,
    render_diff,
)


def _row(subject: str, day: str, value: float, line: int) -> Row:
    return Row(
        subject_id=subject,
        visit_date=date.fromisoformat(day),
        value=float(value),
        line_no=line,
    )


def test_identical_visits_cancel_completely():
    old = [_row("P1", "2026-01-01", 1.0, 2), _row("P2", "2026-02-02", 2.0, 3)]
    new = [_row("P2", "2026-02-02", 2.0, 4), _row("P1", "2026-01-01", 1.0, 5)]
    result = compute_diff(old, new)
    assert result.identical_count == 2
    assert result.changes == []
    assert result.ambiguous is False
    assert result.counts() == {ADDED: 0, REMOVED: 0, CORRECTED: 0, AMBIGUOUS: 0}


def test_unique_addition_removal_and_correction_listed_with_line_numbers():
    old = [
        _row("P1", "2026-01-01", 1.0, 2),
        _row("P1", "2026-02-01", 2.0, 3),
        _row("P2", "2026-01-05", 3.0, 4),
    ]
    new = [
        _row("P1", "2026-01-01", 1.5, 2),   # corrected (unique 1:1 pair)
        _row("P1", "2026-02-01", 2.0, 3),   # identical -> cancels
        _row("P3", "2026-03-01", 9.0, 5),   # added
        # P2 missing -> removed
    ]
    result = compute_diff(old, new)
    assert result.identical_count == 1
    by_key = {(c.kind, c.subject_id): c for c in result.changes}
    assert set(by_key) == {
        (CORRECTED, "P1"),
        (ADDED, "P3"),
        (REMOVED, "P2"),
    }
    correction = by_key[(CORRECTED, "P1")]
    assert correction.old_value == 1.0 and correction.new_value == 1.5
    assert correction.old_line_no == 2 and correction.new_line_no == 2
    assert by_key[(ADDED, "P3")].new_line_no == 5
    assert by_key[(REMOVED, "P2")].old_line_no == 4
    assert result.ambiguous is False


def test_same_subject_same_day_multiple_values_ambiguous_with_both_line_numbers():
    # Old side has two measurements for P1 on the same day, new side has two
    # different ones: no unique pairing, must not be guessed by row order.
    old = [
        _row("P1", "2026-01-01", 1.0, 2),
        _row("P1", "2026-01-01", 2.0, 3),
    ]
    new = [
        _row("P1", "2026-01-01", 3.0, 2),
        _row("P1", "2026-01-01", 4.0, 3),
    ]
    result = compute_diff(old, new)
    assert result.identical_count == 0
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind == AMBIGUOUS
    assert change.ambiguous_old_lines == [2, 3]
    assert change.ambiguous_new_lines == [2, 3]
    assert change.ambiguous_old_values == [1.0, 2.0]
    assert change.ambiguous_new_values == [3.0, 4.0]
    assert result.ambiguous is True


def test_ambiguous_when_one_old_could_match_two_new():
    old = [_row("P1", "2026-01-01", 1.0, 2)]
    new = [
        _row("P1", "2026-01-01", 5.0, 2),
        _row("P1", "2026-01-01", 6.0, 3),
    ]
    result = compute_diff(old, new)
    assert len(result.changes) == 1 and result.changes[0].kind == AMBIGUOUS
    assert result.changes[0].ambiguous_old_lines == [2]
    assert result.changes[0].ambiguous_new_lines == [2, 3]


def test_exact_duplicates_cancel_with_multiplicity_then_remainder_is_removal():
    # Two identical rows on both sides cancel; the third old-only duplicate is
    # uniquely attributable (same value) -> removal, not ambiguity.
    old = [
        _row("P1", "2026-01-01", 1.0, 2),
        _row("P1", "2026-01-01", 1.0, 3),
        _row("P1", "2026-01-01", 1.0, 4),
    ]
    new = [
        _row("P1", "2026-01-01", 1.0, 7),
        _row("P1", "2026-01-01", 1.0, 8),
    ]
    result = compute_diff(old, new)
    assert result.identical_count == 2
    assert len(result.changes) == 1
    assert result.changes[0].kind == REMOVED
    assert result.changes[0].old_line_no == 4


def test_render_diff_contains_only_pseudonyms_and_shifted_dates():
    old = [_row("P1", "2026-01-01", 1.0, 2)]
    new = [
        _row("P1", "2026-01-01", 9.0, 2),
        _row("P2", "2026-02-02", 7.0, 3),
    ]
    result = compute_diff(old, new)
    identity = {"P1": ("S000001", 10), "P2": ("S000002", -5)}
    view = render_diff(result, identity, "cohort-a", 2, 1)

    import json

    serialised = json.dumps(view, ensure_ascii=False)
    # No original identity or original date anywhere in the wire payload.
    for token in ("P1", "P2", "2026-01-01", "2026-02-02"):
        assert token not in serialised
    assert view["qc_ambiguous"] is False
    assert view["base_revision"] == 1 and view["revision"] == 2
    kinds = {c["kind"] for c in view["changes"]}
    assert kinds == {CORRECTED, ADDED}
    shifted = {c["pseudonym"]: c["visit_date_shifted"] for c in view["changes"]}
    assert shifted["S000001"] == "2026-01-11"  # +10 days
    assert shifted["S000002"] == "2026-01-28"  # -5 days
    corrected = next(c for c in view["changes"] if c["kind"] == CORRECTED)
    assert corrected["old_line_no"] == 2 and corrected["new_line_no"] == 2


def test_render_diff_ambiguous_carry_line_numbers_but_no_identity():
    old = [_row("P1", "2026-01-01", 1.0, 2), _row("P1", "2026-01-01", 2.0, 3)]
    new = [_row("P1", "2026-01-01", 8.0, 2), _row("P1", "2026-01-01", 9.0, 3)]
    view = render_diff(
        compute_diff(old, new), {"P1": ("S000007", 17)}, "cohort-z", 3, 2
    )
    import json

    payload = json.dumps(view)
    assert "P1" not in payload and "2026-01-01" not in payload
    assert view["qc_ambiguous"] is True
    change = view["changes"][0]
    assert change["visit_date_shifted"] == "2026-01-18"
    assert change["kind"] == AMBIGUOUS
    assert [c["line_no"] for c in change["old_candidates"]] == [2, 3]
    assert [c["line_no"] for c in change["new_candidates"]] == [2, 3]
    assert [c["value"] for c in change["old_candidates"]] == [1.0, 2.0]
