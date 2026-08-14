from __future__ import annotations

import httpx2

from omarag_bridge.models.api import IngestRequest, RunRequest


def test_book_v2_and_adaptive_query_options_are_safe_defaults() -> None:
    ingest = IngestRequest.model_validate({"sources": [{"type": "file", "path": "/tmp/book.pdf"}]})
    assert ingest.indexing.pipeline == "book-v2"
    assert ingest.indexing.enrichment == "captions"
    assert ingest.indexing.llm_fallback == "auto"

    run = RunRequest.model_validate({"question": "Was ist Beton?"})
    assert run.options.profile == "auto"
    assert run.options.memory == "auto"
    assert run.options.max_sources is None


def test_deep_deadline_is_reserved_for_analysis() -> None:
    invalid = RunRequest.model_validate
    try:
        invalid(
            {
                "mode": "rag",
                "question": "Vergleiche alle Kapitel",
                "options": {"deadline_ms": 60000},
            }
        )
    except ValueError as exc:
        assert "35000" in str(exc)
    else:  # pragma: no cover - makes a missing validator an explicit failure
        raise AssertionError("rag accepted the analysis-only deadline")

    analysis = RunRequest.model_validate(
        {
            "mode": "analysis",
            "question": "Vergleiche alle Kapitel",
            "options": {"profile": "deep", "deadline_ms": 60000},
        }
    )
    assert analysis.options.deadline_ms == 60000


async def test_workspace_readiness_exposes_residency_without_claiming_slo(
    client: httpx2.AsyncClient, workspace: dict[str, object]
) -> None:
    response = await client.get(f"/v1/workspaces/{workspace['id']}/readiness")
    assert response.status_code == 200
    payload = response.json()
    assert payload["required_loaded_models"] == 2
    assert payload["latency_status"] in {"ready", "latency_degraded"}
    assert payload["checks"]["required_concurrent_residency"] == 2


async def test_run_and_search_reject_unsafe_free_form_options(
    client: httpx2.AsyncClient, workspace: dict[str, object]
) -> None:
    workspace_id = str(workspace["id"])
    run = await client.post(
        f"/v1/workspaces/{workspace_id}/runs",
        json={"question": "Beton", "options": {"candidate_k": 1000}},
    )
    assert run.status_code == 422

    search = await client.post(
        f"/v1/workspaces/{workspace_id}/search",
        json={"query": "Beton", "options": {"threshold": 0.0}},
    )
    assert search.status_code == 422
