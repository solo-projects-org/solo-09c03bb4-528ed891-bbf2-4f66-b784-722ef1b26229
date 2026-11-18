"""API and lifecycle tests for revision-difference QC and the held state."""
from __future__ import annotations

import io
import json

import pytest


def _csv_bytes(header: str, rows: list[tuple]) -> bytes:
    buf = io.StringIO()
    buf.write(header + "\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    return buf.getvalue().encode("utf-8")


def _upload(client, cohort: str, revision: str, payload: bytes):
    return client.post(
        f"/api/cohorts/{cohort}/revisions/{revision}",
        files={"file": ("rev.csv", payload, "text/csv")},
    )


def _wait_published(app_env, cohort: str, max_ticks: int = 5):
    tick = app_env["tick"]
    client = app_env["client"]
    for _ in range(max_ticks):
        status = client.get(f"/api/cohorts/{cohort}").json()
        if status["current_package"]["status"] == "published":
            return status
        tick()
    raise AssertionError("package did not reach published state")


BASE = [
    ("P001", "2026-01-10", 12.5),
    ("P001", "2026-02-10", 13.0),
    ("P002", "2026-01-15", 7.2),
]


def test_first_revision_has_no_diff_and_follows_original_flow(app_env):
    client = app_env["client"]
    resp = _upload(client, "cohort-f", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    assert resp.status_code == 202
    body = resp.json()
    assert body["qc"] == {
        "checked": False,
        "ambiguous": False,
        "base_revision": None,
        "package_status": "pending",
        "summary": None,
    }
    assert client.get("/api/cohorts/cohort-f/revisions/1/diff").status_code == 404
    _wait_published(app_env, "cohort-f")
    assert client.get("/api/cohorts/cohort-f/package").status_code == 200


def test_clean_revision_diff_is_listed_and_deidentified(app_env):
    client, tick = app_env["client"], app_env["tick"]
    _upload(client, "cohort-g", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    _wait_published(app_env, "cohort-g")

    rev2 = [
        ("P001", "2026-01-10", 12.9),  # corrected
        ("P001", "2026-02-10", 13.0),  # identical
        ("P003", "2026-03-01", 5.0),   # added; P002 removed
    ]
    resp = _upload(client, "cohort-g", "2", _csv_bytes("subject_id,visit_date,value", rev2))
    assert resp.status_code == 202
    qc = resp.json()["qc"]
    assert qc["checked"] is True and qc["ambiguous"] is False
    assert qc["summary"] == {
        "identical_visits": 1, "added": 1, "removed": 1,
        "corrected": 1, "ambiguous": 0, "total_changes": 3,
    }

    diff = client.get("/api/cohorts/cohort-g/revisions/2/diff")
    assert diff.status_code == 200
    data = diff.json()
    assert data["base_revision"] == "1"
    kinds = {c["kind"] for c in data["changes"]}
    assert kinds == {"added", "removed", "corrected"}
    correction = next(c for c in data["changes"] if c["kind"] == "corrected")
    assert correction["old_line_no"] == 2 and correction["new_line_no"] == 2
    assert correction["old_value"] == 12.5 and correction["new_value"] == 12.9

    # The diff endpoint must never expose original identity or original dates.
    raw = json.dumps(data).encode("utf-8")
    for token in (b"P001", b"P002", b"P003", b"2026-01-10", b"2026-02-10",
                  b"2026-01-15", b"2026-03-01"):
        assert token not in raw
    # Status view carries the QC summary markers.
    status = client.get("/api/cohorts/cohort-g").json()
    assert status["current_package"]["qc_ambiguous"] is False
    assert status["current_package"]["diff_base_revision"] == "1"
    assert status["current_diff"]["summary"]["ambiguous"] == 0
    assert {d["revision"] for d in status["diffs"]} == {"2"}


def test_ambiguous_revision_held_not_claimable_not_downloadable(app_env):
    client, tick = app_env["client"], app_env["tick"]
    _upload(client, "cohort-h", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    _wait_published(app_env, "cohort-h")
    # Old package is initially downloadable.
    assert client.get("/api/cohorts/cohort-h/package").status_code == 200

    # Same subject/day with two values on both sides -> cannot pair uniquely.
    ambiguous = [
        ("P001", "2026-01-10", 1.0),
        ("P001", "2026-01-10", 2.0),
        ("P001", "2026-02-10", 13.0),
        ("P002", "2026-01-15", 7.2),
    ]
    resp = _upload(client, "cohort-h", "2",
                   _csv_bytes("subject_id,visit_date,value", ambiguous))
    assert resp.status_code == 202
    qc = resp.json()["qc"]
    assert qc["ambiguous"] is True and qc["package_status"] == "held"

    status = client.get("/api/cohorts/cohort-h").json()
    states = {p["revision"]: p["status"] for p in status["packages"]}
    assert states["1"] == "superseded"
    assert states["2"] == "held"
    assert status["current_package"]["qc_ambiguous"] is True

    # Held package: not downloadable, not retryable, never claimed by worker.
    assert client.get("/api/cohorts/cohort-h/package").status_code == 409
    assert client.post("/api/cohorts/cohort-h/retry").status_code == 409
    assert tick() is False

    # The diff reports both sides' CSV line numbers, never row-order guessing.
    diff = client.get("/api/cohorts/cohort-h/revisions/2/diff").json()
    assert diff["qc_ambiguous"] is True
    amb = next(c for c in diff["changes"] if c["kind"] == "ambiguous")
    assert [c["line_no"] for c in amb["old_candidates"]] == [2]
    assert [c["line_no"] for c in amb["new_candidates"]] == [2, 3]
    raw = json.dumps(diff).encode("utf-8")
    for token in (b"P001", b"P002", b"2026-01-10", b"2026-02-10", b"2026-01-15"):
        assert token not in raw

    # No package artifact may exist for a held revision.
    pkg_dir = app_env["modules"]["config"].PACKAGE_DIR / "cohort-h"
    assert not (pkg_dir / "rev2_deid.zip").exists()


def test_corrected_revision_passes_qc_and_restores_download(app_env):
    client, tick = app_env["client"], app_env["tick"]
    _upload(client, "cohort-i", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    _wait_published(app_env, "cohort-i")
    ambiguous = [
        ("P001", "2026-01-10", 1.0),
        ("P001", "2026-01-10", 2.0),
        ("P001", "2026-02-10", 13.0),
        ("P002", "2026-01-15", 7.2),
    ]
    _upload(client, "cohort-i", "2", _csv_bytes("subject_id,visit_date,value", ambiguous))
    assert client.get("/api/cohorts/cohort-i/package").status_code == 409

    # Data team resolves the ambiguity: rev3 is fully attributable -> pending.
    corrected = [
        ("P001", "2026-01-10", 1.0),
        ("P001", "2026-02-10", 13.0),
        ("P002", "2026-01-15", 7.2),
    ]
    resp = _upload(client, "cohort-i", "3",
                   _csv_bytes("subject_id,visit_date,value", corrected))
    assert resp.status_code == 202
    assert resp.json()["qc"]["ambiguous"] is False
    states = {p["revision"]: p["status"]
              for p in client.get("/api/cohorts/cohort-i").json()["packages"]}
    assert states["2"] == "superseded" and states["3"] == "pending"

    _wait_published(app_env, "cohort-i")
    assert client.get("/api/cohorts/cohort-i/package").status_code == 200


def test_held_state_survives_restart_and_never_releases(app_env):
    client = app_env["client"]
    _upload(client, "cohort-j", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    _wait_published(app_env, "cohort-j")
    ambiguous = [
        ("P001", "2026-01-10", 1.0),
        ("P001", "2026-01-10", 2.0),
        ("P001", "2026-02-10", 13.0),
        ("P002", "2026-01-15", 7.2),
    ]
    _upload(client, "cohort-j", "2", _csv_bytes("subject_id,visit_date,value", ambiguous))

    # Startup recovery must leave the held revision exactly where it was.
    recovered = app_env["modules"]["database"].reset_interrupted_packages()
    assert recovered == 0
    status = client.get("/api/cohorts/cohort-j").json()
    assert status["current_package"]["status"] == "held"
    assert client.get("/api/cohorts/cohort-j/package").status_code == 409
    # And the worker still refuses to claim it after "restart".
    assert app_env["tick"]() is False


def test_held_revision_does_not_block_other_cohorts(app_env):
    client, tick = app_env["client"], app_env["tick"]
    _upload(client, "cohort-k1", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    _wait_published(app_env, "cohort-k1")
    ambiguous = [
        ("P001", "2026-01-10", 1.0),
        ("P001", "2026-01-10", 2.0),
        ("P001", "2026-02-10", 13.0),
        ("P002", "2026-01-15", 7.2),
    ]
    _upload(client, "cohort-k1", "2", _csv_bytes("subject_id,visit_date,value", ambiguous))

    # Another cohort keeps flowing through the queue while k1 is held.
    _upload(client, "cohort-k2", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    assert tick() is True  # k2 builds; k1's held package is skipped
    assert client.get("/api/cohorts/cohort-k2/package").status_code == 200
    assert client.get("/api/cohorts/cohort-k1/package").status_code == 409


def test_diff_endpoint_validation_and_missing_entities(app_env):
    client = app_env["client"]
    _upload(client, "cohort-l", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    # Unknown cohort / unknown revision.
    assert client.get("/api/cohorts/nope/revisions/1/diff").status_code == 404
    assert client.get("/api/cohorts/cohort-l/revisions/9/diff").status_code == 404
    # Unsafe identifiers are rejected before touching the database.
    assert client.get("/api/cohorts/bad cohort/revisions/1/diff").status_code == 400


def test_ambiguous_diff_against_held_baseline_still_computes(app_env):
    """A revision following a held one diffs against the held current revision."""
    client = app_env["client"]
    _upload(client, "cohort-m", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    _wait_published(app_env, "cohort-m")
    ambiguous2 = [
        ("P001", "2026-01-10", 1.0),
        ("P001", "2026-01-10", 2.0),
        ("P001", "2026-02-10", 13.0),
        ("P002", "2026-01-15", 7.2),
    ]
    _upload(client, "cohort-m", "2", _csv_bytes("subject_id,visit_date,value", ambiguous2))

    clean3 = [
        ("P001", "2026-01-10", 1.0),
        ("P001", "2026-02-10", 13.0),
        ("P002", "2026-01-15", 7.2),
        ("P004", "2026-04-01", 4.4),
    ]
    resp = _upload(client, "cohort-m", "3",
                   _csv_bytes("subject_id,visit_date,value", clean3))
    assert resp.status_code == 202
    diff = client.get("/api/cohorts/cohort-m/revisions/3/diff").json()
    # Baseline is the held rev2; the changes are uniquely attributable.
    assert diff["base_revision"] == "2"
    assert diff["qc_ambiguous"] is False
    assert diff["summary"]["removed"] == 1 and diff["summary"]["added"] == 1


def test_late_build_held_interleaving_does_not_publish(app_env, monkeypatch):
    """A clean rev2 build in flight when an ambiguous rev3 lands must not publish."""
    client, tick = app_env["client"], app_env["tick"]
    services = app_env["modules"]["services"]
    _upload(client, "cohort-n", "1", _csv_bytes("subject_id,visit_date,value", BASE))
    assert tick() is True

    clean2 = BASE + [("P009", "2026-05-01", 1.5)]
    _upload(client, "cohort-n", "2", _csv_bytes("subject_id,visit_date,value", clean2))
    claim = services.claim_pending_package()
    assert claim == ("cohort-n", 2)

    # Ambiguous rev3 lands while rev2's build is in progress: rev2 superseded,
    # rev3 held. The late rev2 result must be discarded, nothing publishes.
    ambiguous3 = [
        ("P001", "2026-01-10", 1.0),
        ("P001", "2026-01-10", 2.0),
        ("P001", "2026-02-10", 13.0),
        ("P002", "2026-01-15", 7.2),
        ("P009", "2026-05-01", 1.5),
    ]
    _upload(client, "cohort-n", "3", _csv_bytes("subject_id,visit_date,value", ambiguous3))
    with pytest.raises(services.StaleRevisionError):
        services.finalize_package("cohort-n", 2)

    status = client.get("/api/cohorts/cohort-n").json()
    states = {p["revision"]: p["status"] for p in status["packages"]}
    assert states["2"] == "superseded"
    assert states["3"] == "held"
    assert client.get("/api/cohorts/cohort-n/package").status_code == 409
    assert tick() is False  # nothing claimable while held
    pkg_dir = app_env["modules"]["config"].PACKAGE_DIR / "cohort-n"
    assert not (pkg_dir / "rev2_deid.zip").exists()
