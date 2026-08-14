from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI

from omarag_bridge.models.book import (
    BookRagGraph,
    BookStructure,
    BookStructureNode,
    EvidenceAnchor,
    EvidenceRecord,
)
from omarag_bridge.models.domain import Citation, JobStatus
from omarag_bridge.models.media import MediaAsset, MediaText, NormalizedMediaBBox
from omarag_bridge.services import run_service as run_service_module
from omarag_bridge.services.book_snapshot_service import build_book_knowledge_snapshot
from omarag_bridge.services.media_service import build_media_snapshot
from omarag_bridge.services.model_service import ModelService
from omarag_bridge.services.ollama_stream import OllamaModelIdentity


async def test_v11_hardware_profiles_are_read_only_explicit_and_applicable(
    client: httpx2.AsyncClient,
    workspace: dict[str, object],
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanned = await client.get("/v1/models/hardware/scan")
    assert scanned.status_code == 200
    scan_payload = scanned.json()
    assert scan_payload["schema_version"] == 1
    assert scan_payload["tier"] == 4
    assert scan_payload["profile"] == "normal"
    assert {item["role"] for item in scan_payload["recommendations"]} >= {
        "chat",
        "vl",
        "embedding",
        "rerank",
    }

    quality = await client.get("/v1/models/recommendation", params={"profile": "quality"})
    assert quality.status_code == 200
    assert quality.json()["tier"] == 4
    assert quality.json()["profile"] == "quality"
    assert quality.json()["catalog_version"] == scan_payload["catalog_version"]

    detailed_scan = await client.post("/v1/models/hardware/scan", json={"force": True})
    assert detailed_scan.status_code == 200
    assert detailed_scan.json()["schema_version"] == 2
    assert detailed_scan.json()["cpu_model"] == "Test CPU"
    assert detailed_scan.json()["capacity_tier"] == 4

    rejected_benchmark = await client.post(
        "/v1/models/hardware/benchmark",
        json={"profile": "fast", "tier": 3, "confirm": "yes"},
    )
    assert rejected_benchmark.status_code == 422
    benchmark = await client.post(
        "/v1/models/hardware/benchmark",
        json={"profile": "fast", "tier": 3, "confirm": "BENCHMARK"},
    )
    assert benchmark.status_code == 200
    assert benchmark.json()["tested_tier"] == 3
    assert benchmark.json()["passed"] is True

    workspace_id = str(workspace["id"])
    recommendation = await client.post(
        "/v1/models/recommendation",
        json={"performance_profile": "normal", "workspace_id": workspace_id},
    )
    assert recommendation.status_code == 200
    assert recommendation.json()["recommendation_id"] == "rec-test-4-normal"
    assert recommendation.json()["ready_now"] is True
    assert recommendation.json()["retrieval_budgets"]["complex"]["max_images"] == 4

    asserted_tier = await client.post(
        f"/v1/workspaces/{workspace_id}/model-profile/preflight",
        json={"performance_profile": "normal", "benchmark_tier": 8},
    )
    assert asserted_tier.status_code == 409

    preflight = await client.post(
        f"/v1/workspaces/{workspace_id}/model-profile/preflight",
        json={"performance_profile": "quality", "workspace_id": workspace_id},
    )
    assert preflight.status_code == 200
    preflight_payload = preflight.json()
    assert preflight_payload["recommendation"]["recommendation_id"] == "rec-test-4-quality"
    assert preflight_payload["downloads"] == []
    assert preflight_payload["requires_reindex"] is False
    assert preflight_payload["can_apply"] is True

    second_workspace = await client.post("/v1/workspaces", json={"name": "Zweite Bibliothek"})
    second_workspace_id = second_workspace.json()["id"]
    second_preflight = await client.post(
        f"/v1/workspaces/{second_workspace_id}/model-profile/preflight",
        json={"performance_profile": "quality"},
    )
    assert second_preflight.status_code == 200
    assert (
        second_preflight.json()["recommendation"]["recommendation_id"]
        == preflight_payload["recommendation"]["recommendation_id"]
    )

    missing_confirmation = await client.post(
        f"/v1/workspaces/{workspace_id}/model-profile/apply",
        json={
            "preflight_id": preflight_payload["recommendation"]["recommendation_id"],
            "confirm": "yes",
        },
    )
    assert missing_confirmation.status_code == 422
    applied = await client.post(
        f"/v1/workspaces/{workspace_id}/model-profile/apply",
        json={
            "preflight_id": preflight_payload["recommendation"]["recommendation_id"],
            "confirm": "APPLY",
        },
    )
    assert applied.status_code == 200
    assert applied.headers["etag"] == f'"{applied.json()["etag"]}"'
    config = applied.json()["content"]
    assert "model_profile:" in config
    assert "catalog_id:" in config
    assert "performance_profile: quality" in config
    assert "expert_mode: false" in config
    assert "artifacts:" in config
    assert "digest:" in config

    settings = app.state.services.features.configured_model_settings(workspace_id)
    profile = settings["profile"]
    assert isinstance(profile, dict)
    artifacts = profile["artifacts"]
    assert isinstance(artifacts, dict)

    class DriftedOllama:
        def __init__(self, _: str) -> None:
            pass

        async def __aenter__(self) -> DriftedOllama:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_models(self) -> list[OllamaModelIdentity]:
            return [
                OllamaModelIdentity(
                    name=str(settings["chat"]),
                    digest="sha256:catalog-drift",
                    size=1,
                ),
                OllamaModelIdentity(
                    name=str(settings["embedding"]),
                    digest=str(artifacts["embedding"]["digest"]),
                    size=1,
                ),
            ]

        async def running_models(self) -> list[object]:
            return []

    monkeypatch.setattr(run_service_module, "OllamaStreamClient", DriftedOllama)
    with pytest.raises(RuntimeError, match="differs from the applied model catalog"):
        await app.state.services.runs._query_runtime_identity(workspace_id)

    class DriftedEmbedding(DriftedOllama):
        async def list_models(self) -> list[OllamaModelIdentity]:
            return [
                OllamaModelIdentity(
                    name=str(settings["embedding"]),
                    digest="sha256:embedding-drift",
                    size=1,
                )
            ]

    monkeypatch.setattr(run_service_module, "OllamaStreamClient", DriftedEmbedding)
    with pytest.raises(RuntimeError, match="differs from the applied model catalog"):
        await app.state.services.runs._query_runtime_identity(workspace_id, require_generator=False)


async def test_request_runtime_pins_expert_roles_and_marks_unknown_reranker_uncalibrated(
    workspace: dict[str, object],
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = str(workspace["id"])
    runs = app.state.services.runs
    settings = app.state.services.features.configured_model_settings(workspace_id)

    class StableOllama:
        def __init__(self, _: str) -> None:
            pass

        async def __aenter__(self) -> StableOllama:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_models(self) -> list[OllamaModelIdentity]:
            return [
                OllamaModelIdentity(str(settings["chat"]), "generator-digest", 1),
                OllamaModelIdentity(str(settings["embedding"]), "embedding-digest", 1),
            ]

        async def running_models(self) -> list[object]:
            return []

    monkeypatch.setattr(run_service_module, "OllamaStreamClient", StableOllama)
    monkeypatch.setattr(
        ModelService, "_hugging_face_revision_present", classmethod(lambda *_: True)
    )

    identity, metadata = await runs._query_runtime_identity(workspace_id, require_vl=True)

    assert identity is not None and identity.digest == "generator-digest"
    pins = metadata["model_identities"]
    assert pins["generator"]["provider"] == "ollama"
    assert pins["embedding"]["digest"] == "embedding-digest"
    assert pins["vl"] == pins["generator"]
    assert pins["reranker"]["revision"]
    assert pins["reranker"]["digest"]

    original_roles = runs.model_roles
    original_settings = runs.model_settings
    assert original_roles is not None and original_settings is not None
    runs.model_roles = lambda _: {**original_roles(workspace_id), "vl": "other-vl:model"}
    with pytest.raises(RuntimeError, match="VL role is not the digest-pinned QA model"):
        await runs._query_runtime_identity(workspace_id, require_vl=True)

    runs.model_roles = lambda _: {**original_roles(workspace_id), "rerank": "org/custom"}
    runs.model_settings = lambda _: {
        **original_settings(workspace_id),
        "rerank": "org/custom",
        "rerank_revision": None,
        "rerank_digest": None,
    }
    _, uncalibrated = await runs._query_runtime_identity(workspace_id)
    assert uncalibrated["model_identities"]["reranker"]["status"] == "uncalibrated"
    with pytest.raises(RuntimeError, match="immutable local revision pin"):
        await runs._query_runtime_identity(workspace_id, allow_uncalibrated_reranker=False)


def _index_media_fixture(app: FastAPI, workspace_id: str) -> tuple[str, bytes]:
    logical_document_id = "book-media"
    generation_id = "gen-media"
    fingerprint = "f" * 64
    evidence_id = "ev-media"
    media_id = "media-diagram"
    structure = BookStructure(
        logical_document_id=logical_document_id,
        mode="body-headings",
        confidence=0.9,
        total_pages=3,
        nodes=[
            BookStructureNode(
                node_id="sec-media",
                depth=0,
                ordinal=0,
                title="Wasserzementwert",
                normalized_title="wasserzementwert",
                page_start=1,
                page_end=3,
                source_kind="body-heading",
                confidence=0.9,
            )
        ],
        page_labels={"2": 2},
    )
    evidence = EvidenceRecord(
        evidence_id=evidence_id,
        raw_content="Das Diagramm zeigt den Wasserzementwert.",
        content_hash="content-media",
        anchors=[
            EvidenceAnchor(
                page_no=2,
                source_ref="#/pictures/1",
                bbox=(20.0, 180.0, 160.0, 20.0),
                label="picture",
            )
        ],
        page_start=2,
        page_end=2,
        section_node_id="sec-media",
        labels=["picture"],
    )
    crop_bytes = b"RIFF-test-webp-crop"
    asset = MediaAsset(
        media_id=media_id,
        logical_document_id=logical_document_id,
        generation_id=generation_id,
        source_fingerprint=fingerprint,
        page_no=2,
        page_label="2",
        doc_item_ref="#/pictures/1",
        bbox=NormalizedMediaBBox(x0=0.1, y0=0.1, x1=0.8, y1=0.8),
        kind="diagram",
        section_node_id="sec-media",
        crop_version="omarag-media-crop-v1",
        captions=[
            MediaText(
                text="Wasserzementwert im Verlauf",
                origin="native-caption",
                source_ref="#/texts/caption-1",
            )
        ],
        nearby_text=[
            MediaText(
                text="Das Diagramm zeigt den Wasserzementwert.",
                origin="nearby-text",
                source_ref="#/pictures/1",
                evidence_id=evidence_id,
            )
        ],
        evidence_ids=[evidence_id],
        pixel_sha256="a" * 64,
        perceptual_hash="0" * 16,
        width_px=900,
        height_px=600,
        mime_type="image/webp",
        crop_resource=f"media/blobs/{'a' * 64}.webp",
        thumbnail_resource=f"media/thumbnails/{'a' * 64}.webp",
    )
    media = build_media_snapshot(
        structure=structure,
        evidence=[evidence],
        assets=[asset],
    )
    snapshot = build_book_knowledge_snapshot(
        logical_document_id=logical_document_id,
        generation_id=generation_id,
        fingerprint=fingerprint,
        config_hash="config-media",
        structure=structure,
        evidence=[evidence],
        graph=BookRagGraph(),
        media=media,
    )
    services = app.state.services
    services.store.upsert_document(
        workspace_id,
        "/books/media.pdf",
        fingerprint,
        {
            "logical_document_id": logical_document_id,
            "generation_id": generation_id,
            "original_source": "/books/media.pdf",
            "managed_source": "/managed/media.pdf",
            "pipeline_version": "book-v2-test",
            "book_metadata": {"title": "Baustoffkunde"},
            "quality": {},
            "segments": [
                {
                    "segment_index": 0,
                    "document_id": "segment-media",
                    "page_start": 1,
                    "page_end": 3,
                }
            ],
            "chunk_manifest": [
                {
                    "segment_index": 0,
                    "chunk_id": "chunk-media",
                    "chunk_order": 0,
                    "content_hash": "content-media",
                    "pages": [2],
                    "evidence_id": evidence_id,
                    "generation_id": generation_id,
                }
            ],
            "book_knowledge_snapshot": snapshot.model_dump(mode="json"),
        },
    )
    manifest = services.workspaces.get(workspace_id)
    database = Path(manifest.path) / "database"
    crop = database / "media" / "blobs" / f"{'a' * 64}.webp"
    thumbnail = database / "media" / "thumbnails" / f"{'a' * 64}.webp"
    crop.parent.mkdir(parents=True, exist_ok=True)
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    crop.write_bytes(crop_bytes)
    thumbnail.write_bytes(b"RIFF-test-webp-thumbnail")

    services.store.create_run(
        "run-media-api",
        workspace_id,
        {
            "session_id": "session-media-api",
            "question": "Zeige das Diagramm zum Wasserzementwert",
            "evidence_mode": "normal",
        },
    )
    services.store.update_run(
        "run-media-api",
        status=JobStatus.COMPLETED,
        answer="Das Diagramm befindet sich auf Seite 2.",
        citations=[
            Citation(
                evidence_id=evidence_id,
                generation_id=generation_id,
                chunk_id="chunk-media",
                logical_document_id=logical_document_id,
                document_title="Baustoffkunde",
                pages=[2],
                picture_refs=["#/pictures/1"],
                excerpt="Das Diagramm zeigt den Wasserzementwert.",
                relevance_score=0.95,
            )
        ],
    )
    return media_id, crop_bytes


async def test_v11_visual_evidence_exposes_pages_then_real_crops_and_okf(
    client: httpx2.AsyncClient,
    app: FastAPI,
    workspace: dict[str, Any],
) -> None:
    workspace_id = str(workspace["id"])
    app.state.services.store.create_run(
        "run-media-pending",
        workspace_id,
        {
            "session_id": "session-media-pending",
            "question": "Noch nicht fertig",
            "evidence_mode": "normal",
        },
    )
    pending = await client.get("/v1/runs/run-media-pending/visual-evidence")
    assert pending.status_code == 409
    assert app.state.services.store.get_run_visual_evidence("run-media-pending") is None

    media_id, crop_bytes = _index_media_fixture(app, workspace_id)

    app.state.services.store.create_run(
        "run-media-stale-generation",
        workspace_id,
        {
            "session_id": "session-media-stale",
            "question": "Zeige das Diagramm",
            "evidence_mode": "normal",
        },
    )
    app.state.services.store.update_run(
        "run-media-stale-generation",
        status=JobStatus.COMPLETED,
        answer="Seite 2.",
        citations=[
            Citation(
                evidence_id="ev-media",
                generation_id="gen-answered-before-reindex",
                chunk_id="chunk-media",
                logical_document_id="book-media",
                pages=[2],
                excerpt="Das Diagramm zeigt den Wasserzementwert.",
            )
        ],
    )
    stale_visual = await client.get("/v1/runs/run-media-stale-generation/visual-evidence")
    assert stale_visual.status_code == 200
    assert stale_visual.json()["pages"]
    assert stale_visual.json()["media"] == []
    assert app.state.services.store.get_run_visual_evidence("run-media-stale-generation") is None

    document_media = await client.get(f"/v1/workspaces/{workspace_id}/documents/book-media/media")
    assert document_media.status_code == 200
    assert [item["media_id"] for item in document_media.json()] == [media_id]
    assert document_media.json()[0]["kind"] == "diagram"

    search = await client.get(
        f"/v1/workspaces/{workspace_id}/media/search",
        params={"query": "Wasserzementwert", "limit": 4},
    )
    assert search.status_code == 200
    assert [item["media_id"] for item in search.json()] == [media_id]
    assert "lexical_rank" not in search.json()[0]

    item = await client.get(f"/v1/workspaces/{workspace_id}/media/{media_id}")
    assert item.status_code == 200
    assert item.json()["bbox"]["coordinate_space"] == "normalized"

    visual = await client.get("/v1/runs/run-media-api/visual-evidence")
    assert visual.status_code == 200
    visual_payload = visual.json()
    assert visual_payload["schema_version"] == 1
    assert visual_payload["pages"][0]["page"] == 2
    assert visual_payload["pages"][0]["citation_index"] == 0
    assert len(visual_payload["media"]) == 1
    assert len(visual_payload["media"]) <= 4
    assert visual_payload["media"][0]["media_id"] == media_id
    assert visual_payload["media"][0]["chunk_ids"] == ["chunk-media"]
    assert visual_payload["media"][0]["preview_url"].endswith(f"/{'a' * 64}/crop")

    # The second request exercises the persisted lazy result rather than rebuilding it.
    cached = await client.get("/v1/runs/run-media-api/visual-evidence")
    assert cached.json() == visual_payload

    crop = await client.get(f"/v1/workspaces/{workspace_id}/media/{media_id}/crop")
    assert crop.status_code == 200
    assert crop.content == crop_bytes
    assert crop.headers["content-type"] == "image/webp"
    assert crop.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert crop.headers["x-content-type-options"] == "nosniff"

    thumbnail = await client.get(f"/v1/workspaces/{workspace_id}/media/{media_id}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.content != crop_bytes

    stable_crop = await client.get(f"/v1/workspaces/{workspace_id}/media/blobs/{'a' * 64}/crop")
    assert stable_crop.status_code == 200
    assert stable_crop.content == crop_bytes

    okf = await client.get(f"/v1/workspaces/{workspace_id}/media/{media_id}/okf-proposal")
    assert okf.status_code == 200
    okf_payload = okf.json()
    assert okf_payload["type"] == "Book Diagram"
    assert okf_payload["status"] == "draft"
    assert okf_payload["generated"] is None
    assert okf_payload["sources"][0]["page"] == 2
    assert okf_payload["omarag"]["media"]["evidence_ids"] == ["ev-media"]
