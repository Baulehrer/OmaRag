from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from omarag_bridge.app import create_app
from omarag_bridge.config import Settings
from omarag_bridge.models.api import CreateWorkspaceRequest
from omarag_bridge.models.domain import JobStatus


@pytest.mark.asyncio
async def test_startup_sweep_removes_only_stale_inactive_url_import_storage(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", auth_enabled=False))
    services = app.state.services
    workspace = services.workspaces.create(CreateWorkspaceRequest(name="Imports"))
    workspace_root = Path(workspace.path)
    import_root = workspace_root / ".omarag" / "url-imports"
    import_root.mkdir(mode=0o700, parents=True)

    active_job_id = "job-a11ce0000001"
    active, _ = services.store.create_job_idempotent(
        job_id=active_job_id,
        workspace_id=workspace.id,
        kind="ingest",
        payload={"sources": [{"type": "url", "path": "https://books.example.test/a"}]},
        idempotency_key="active-url-import",
    )
    services.store.update_job(active.id, status=JobStatus.PAUSED)

    stale = import_root / "job-dead00000001"
    fresh = import_root / "job-fresh000001"
    active_path = import_root / active_job_id
    for candidate in (stale, fresh, active_path):
        candidate.mkdir(mode=0o700)
        (candidate / ".url-import-partial.pdf").write_bytes(b"private partial")

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_secret = outside / "must-survive.txt"
    outside_secret.write_text("keep", encoding="utf-8")
    link = import_root / "job-1ink00000001"
    link.symlink_to(outside, target_is_directory=True)

    legacy_workspace = services.workspaces.create(CreateWorkspaceRequest(name="Legacy Import"))
    legacy = Path(legacy_workspace.path) / ".omarag-url-import-crash"
    legacy.mkdir(mode=0o700)
    (legacy / ".url-import-partial.pdf").write_bytes(b"legacy partial")

    old = time.time() - 7200
    for candidate in (stale, active_path, legacy):
        os.utime(candidate, (old, old))
    os.utime(link, (old, old), follow_symlinks=False)

    async with app.router.lifespan_context(app):
        pass

    assert not stale.exists()
    assert fresh.is_dir()
    assert active_path.is_dir()
    assert not link.exists(follow_symlinks=False)
    assert outside_secret.read_text(encoding="utf-8") == "keep"
    assert not legacy.exists()
