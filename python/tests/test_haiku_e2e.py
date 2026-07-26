from __future__ import annotations

import os
from pathlib import Path

import pytest

from omarag_bridge.adapters.haiku_v070 import HaikuV070Adapter
from omarag_bridge.models.api import CreateWorkspaceRequest
from omarag_bridge.services.workspace_service import WorkspaceService
from omarag_bridge.store import StateStore

PDF = os.environ.get("OMARAG_E2E_PDF")


@pytest.mark.haiku_e2e
@pytest.mark.skipif(not PDF, reason="OMARAG_E2E_PDF ist nicht gesetzt")
async def test_real_pdf_ingest_search_and_answer(tmp_path: Path) -> None:
    adapter = HaikuV070Adapter()
    assert adapter.available
    store = StateStore(tmp_path / "state.sqlite3")
    workspaces = WorkspaceService(tmp_path / "workspaces", store)
    workspace = workspaces.create(CreateWorkspaceRequest(name="Haiku E2E"))
    database = workspaces.database_path(workspace.id)

    result = await adapter.ingest(database, str(PDF))
    assert result["document_id"]

    hits = await adapter.search(database, "Welche Kennfarbe hat OM-42?", 5)
    assert hits
    assert "Ultramarinblau" in hits[0].content
    assert 1 in hits[0].pages

    answer, citations = await adapter.ask(
        database,
        "Welche Kennfarbe hat OM-42 und welche Mindestueberdeckung verlangt XR7?",
    )
    assert "Ultramarinblau" in answer
    assert "55" in answer
    assert citations
    store.close()
