from __future__ import annotations

from pathlib import Path

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
