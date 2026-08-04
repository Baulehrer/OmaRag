from __future__ import annotations

from pathlib import Path

import pytest

from omarag_bridge.models.api import CreateWorkspaceRequest
from omarag_bridge.models.domain import JobStatus
from omarag_bridge.services.workspace_service import WorkspaceService
from omarag_bridge.store import StateStore


def test_running_job_becomes_resumable_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    workspaces = WorkspaceService(tmp_path / "workspaces", store)
    workspace = workspaces.create(CreateWorkspaceRequest(name="Recovery"))
    job, reused = store.create_job_idempotent(
        job_id="job-recovery",
        workspace_id=workspace.id,
        kind="ingest",
        payload={"sources": []},
        idempotency_key="recovery-key",
    )
    assert reused is False
    store.update_job(job.id, status=JobStatus.RUNNING, phase="embedding")
    store.close()

    reopened = StateStore(database)
    recovered = reopened.get_job(job.id)
    assert recovered.status == JobStatus.PAUSED
    assert recovered.phase == "interrupted"
    reopened.close()


def test_segment_ledger_and_document_fingerprint_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Long books")
    )
    job, _ = store.create_job_idempotent(
        job_id="job-segments",
        workspace_id=workspace.id,
        kind="ingest",
        payload={"sources": [{"path": "/books/daedalus.pdf"}]},
        idempotency_key="segments-key",
    )
    segment = {
        "fingerprint": "abc123",
        "generation_id": "gen-1",
        "segment_index": 0,
        "page_start": 1,
        "page_end": 25,
        "document_id": "haiku-segment-1",
        "metadata": {"cache_hit": True},
    }
    store.record_segment(job.id, 0, segment)
    result = {
        "document_id": "book-1",
        "logical_document_id": "book-1",
        "generation_id": "gen-1",
        "segment_document_ids": ["haiku-segment-1"],
    }
    store.upsert_document(workspace.id, "/books/daedalus.pdf", "abc123", result)
    store.close()

    reopened = StateStore(database)
    assert reopened.list_segments(job.id, 0)[0]["page_end"] == 25
    indexed = reopened.document_by_fingerprint(workspace.id, "abc123")
    assert indexed is not None
    assert indexed["generation_id"] == "gen-1"
    reopened.close()


def test_current_document_policy_selects_latest_active_edition(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Editions")
    )
    for edition in (7, 8):
        store.upsert_document(
            workspace.id,
            f"/books/edition-{edition}.pdf",
            f"fingerprint-{edition}",
            {
                "document_id": f"book-{edition}",
                "generation_id": f"gen-{edition}",
                "book_metadata": {
                    "work_id": "work-concrete",
                    "title": "Baustoffkunde",
                    "edition_number": edition,
                    "document_status": "active",
                    "confirmed": True,
                },
                "segments": [
                    {
                        "document_id": f"segment-{edition}",
                        "segment_index": 0,
                        "page_start": 1,
                        "page_end": 10,
                    }
                ],
            },
        )

    assert store.resolve_segment_ids(workspace.id, {}, "current-only") == ["segment-8"]
    assert store.resolve_segment_ids(workspace.id, {"edition_number": 7}, "current-only") == [
        "segment-7"
    ]
    with pytest.raises(ValueError, match="Unsupported document filters"):
        store.resolve_segment_ids(workspace.id, {"edtion_number": 7}, "current-only")
    with pytest.raises(ValueError, match="Unsupported document policy"):
        store.resolve_segment_ids(workspace.id, {}, "curent-only")
    store.close()
