"""Revision-difference quality control.

When a data team hands over a new revision of a cohort, the service must tell
them apart from the revision currently in force.  The comparison rules are
deliberately conservative because a follow-up CSV can legitimately contain
several measurements of the same subject on the same visit date:

1. Visits that are *exactly* identical (subject, date and value) cancel out
   first, respecting multiplicity.
2. What remains is grouped by (subject, visit date).  Within each group the
   pairing is accepted only when it is unique:

   * rows only on the new side -> ``added``;
   * rows only on the old side -> ``removed``;
   * one old and one new row with different values -> ``corrected``;
   * any group that cannot be paired uniquely -> ``ambiguous``.  No guessing
     by row order is ever performed; both sides' physical CSV line numbers
     are reported so the data team can resolve the conflict manually.

The output of :func:`render_diff` contains only stable pseudonyms and shifted
dates.  Original subject ids and original visit dates stay inside the service
and are never returned through the API or the web page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .csvutil import Row

# Diff outcome categories.
ADDED = "added"
REMOVED = "removed"
CORRECTED = "corrected"
AMBIGUOUS = "ambiguous"

CHANGE_KINDS = (ADDED, REMOVED, CORRECTED, AMBIGUOUS)


@dataclass
class Change:
    """One internal, identity-bearing change record.

    Never serialised towards a client as-is: :func:`render_diff` rewrites the
    identity/date fields into pseudonyms and shifted dates first.
    """

    kind: str
    subject_id: str
    visit_date: Any  # datetime.date
    new_line_no: int | None = None
    old_line_no: int | None = None
    value: float | None = None
    old_value: float | None = None
    new_value: float | None = None
    ambiguous_new_lines: list[int] = field(default_factory=list)
    ambiguous_old_lines: list[int] = field(default_factory=list)
    ambiguous_new_values: list[float] = field(default_factory=list)
    ambiguous_old_values: list[float] = field(default_factory=list)


@dataclass
class DiffResult:
    changes: list[Change]
    identical_count: int

    @property
    def ambiguous(self) -> bool:
        return any(c.kind == AMBIGUOUS for c in self.changes)

    def counts(self) -> dict[str, int]:
        result = {kind: 0 for kind in CHANGE_KINDS}
        for change in self.changes:
            result[change.kind] += 1
        return result


def compute_diff(old_rows: list[Row], new_rows: list[Row]) -> DiffResult:
    """Compare the previously current revision with the incoming one.

    Exact matches (same subject id, same visit date, same value) cancel first
    while preserving multiplicity.  The leftovers are grouped by
    (subject id, visit date) and classified by group cardinality so that
    non-unique pairings are flagged ambiguous instead of guessed.
    """
    old_remaining = list(old_rows)
    new_remaining = list(new_rows)
    identical_count = 0

    # Cancel exactly identical visits, one-for-one, multiplicity aware.
    for new_row in list(new_remaining):
        for idx, old_row in enumerate(old_remaining):
            if (
                old_row.subject_id == new_row.subject_id
                and old_row.visit_date == new_row.visit_date
                and old_row.value == new_row.value
            ):
                old_remaining.pop(idx)
                new_remaining.remove(new_row)
                identical_count += 1
                break

    groups: dict[tuple[str, Any], dict[str, list[Row]]] = {}
    for row in old_remaining:
        groups.setdefault((row.subject_id, row.visit_date), {"old": [], "new": []})[
            "old"
        ].append(row)
    for row in new_remaining:
        groups.setdefault((row.subject_id, row.visit_date), {"old": [], "new": []})[
            "new"
        ].append(row)

    changes: list[Change] = []
    # Deterministic ordering: subject id, then calendar date, then kind.
    for (subject_id, visit_date), bucket in sorted(
        groups.items(), key=lambda item: (item[0][0], item[0][1].toordinal())
    ):
        olds = sorted(bucket["old"], key=lambda r: r.line_no)
        news = sorted(bucket["new"], key=lambda r: r.line_no)

        if not olds:
            for row in news:
                changes.append(
                    Change(
                        kind=ADDED,
                        subject_id=subject_id,
                        visit_date=visit_date,
                        new_line_no=row.line_no,
                        value=row.value,
                    )
                )
        elif not news:
            for row in olds:
                changes.append(
                    Change(
                        kind=REMOVED,
                        subject_id=subject_id,
                        visit_date=visit_date,
                        old_line_no=row.line_no,
                        value=row.value,
                    )
                )
        elif len(olds) == 1 and len(news) == 1:
            old_row, new_row = olds[0], news[0]
            changes.append(
                Change(
                    kind=CORRECTED,
                    subject_id=subject_id,
                    visit_date=visit_date,
                    old_line_no=old_row.line_no,
                    new_line_no=new_row.line_no,
                    old_value=old_row.value,
                    new_value=new_row.value,
                )
            )
        else:
            # Same subject/date with multiple measurements on at least one
            # side: the correspondence cannot be determined uniquely. Report
            # every candidate with both sides' CSV line numbers instead of
            # pairing by row order.
            changes.append(
                Change(
                    kind=AMBIGUOUS,
                    subject_id=subject_id,
                    visit_date=visit_date,
                    ambiguous_old_lines=[r.line_no for r in olds],
                    ambiguous_new_lines=[r.line_no for r in news],
                    ambiguous_old_values=[r.value for r in olds],
                    ambiguous_new_values=[r.value for r in news],
                )
            )

    return DiffResult(changes=changes, identical_count=identical_count)


def render_diff(
    result: DiffResult,
    identity: dict[str, tuple[str, int]],
    cohort_id: str,
    revision_label: int,
    base_revision: int,
) -> dict[str, Any]:
    """Turn an internal diff into a de-identified, JSON-serialisable view.

    Only pseudonyms and per-subject shifted dates are emitted; the original
    subject id and original visit date are not present anywhere in the result.
    """
    rendered: list[dict[str, Any]] = []
    for change in result.changes:
        pseudonym, shift = identity[change.subject_id]
        shifted_date = (change.visit_date + timedelta(days=shift)).isoformat()
        item: dict[str, Any] = {
            "kind": change.kind,
            "pseudonym": pseudonym,
            "visit_date_shifted": shifted_date,
        }
        if change.kind == ADDED:
            item.update(value=change.value, new_line_no=change.new_line_no)
        elif change.kind == REMOVED:
            item.update(value=change.value, old_line_no=change.old_line_no)
        elif change.kind == CORRECTED:
            item.update(
                old_value=change.old_value,
                new_value=change.new_value,
                old_line_no=change.old_line_no,
                new_line_no=change.new_line_no,
            )
        else:  # AMBIGUOUS
            item.update(
                old_candidates=[
                    {"line_no": line_no, "value": value}
                    for line_no, value in zip(
                        change.ambiguous_old_lines, change.ambiguous_old_values
                    )
                ],
                new_candidates=[
                    {"line_no": line_no, "value": value}
                    for line_no, value in zip(
                        change.ambiguous_new_lines, change.ambiguous_new_values
                    )
                ],
            )
        rendered.append(item)

    # Stable, identity-free presentation order.
    rendered.sort(key=lambda item: (item["pseudonym"], item["visit_date_shifted"], item["kind"]))

    counts = result.counts()
    return {
        "cohort_id": cohort_id,
        "revision": revision_label,
        "base_revision": base_revision,
        "qc_ambiguous": result.ambiguous,
        "summary": {
            "identical_visits": result.identical_count,
            "added": counts[ADDED],
            "removed": counts[REMOVED],
            "corrected": counts[CORRECTED],
            "ambiguous": counts[AMBIGUOUS],
            "total_changes": len(result.changes),
        },
        "changes": rendered,
    }
