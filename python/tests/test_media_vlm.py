from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from omarag_bridge.adapters.book_v2 import _configured_vlm_digest, _configured_vlm_model
from omarag_bridge.models.book import BookStructure, BookStructureNode, KnowledgeTerm
from omarag_bridge.models.media import MediaAsset, NormalizedMediaBBox
from omarag_bridge.services.media_service import (
    MediaVlmLimits,
    build_media_snapshot,
    build_okf_media_proposal,
    enrich_media_assets_vlm,
)


def test_configured_vlm_prefers_explicit_workspace_role_then_vision_qa(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        qa=SimpleNamespace(
            model=SimpleNamespace(
                provider="ollama",
                name="qa-vision:4b",
                vision=True,
            )
        )
    )
    path = tmp_path / "haiku.rag.yaml"
    path.write_text(
        "oracle:\n  model_defaults:\n    vl: dedicated-vl:8b\n",
        encoding="utf-8",
    )

    assert _configured_vlm_model(config, config_path=path) == "dedicated-vl:8b"
    assert _configured_vlm_digest(path, "dedicated-vl:8b") is None
    path.write_text(
        "oracle:\n"
        "  model_defaults:\n    vl: dedicated-vl:8b\n"
        "  model_profile:\n    expert_mode: false\n    artifacts:\n"
        "      vl:\n        provider: ollama\n        model: dedicated-vl:8b\n"
        "        digest: sha256:catalog-vl\n",
        encoding="utf-8",
    )
    assert _configured_vlm_digest(path, "dedicated-vl:8b") == "sha256:catalog-vl"
    path.unlink()
    assert _configured_vlm_model(config, config_path=path) == "qa-vision:4b"
    config.qa.model.vision = False
    assert _configured_vlm_model(config, config_path=path) is None


def _pixel_sha256(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    return hashlib.sha256(
        rgb.width.to_bytes(4, "big") + rgb.height.to_bytes(4, "big") + rgb.tobytes()
    ).hexdigest()


def _materialized_asset(
    root: Path,
    *,
    media_id: str = "media-vlm",
    color: str = "navy",
    page_no: int = 1,
) -> MediaAsset:
    image = Image.new("RGB", (64, 48), color)
    digest = _pixel_sha256(image)
    blob = root / "media" / "blobs" / f"{digest}.webp"
    thumbnail = root / "media" / "thumbnails" / f"{digest}.webp"
    blob.parent.mkdir(parents=True, exist_ok=True)
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    image.save(blob, format="WEBP", lossless=True)
    image.save(thumbnail, format="WEBP", lossless=True)
    return MediaAsset(
        media_id=media_id,
        logical_document_id="book-vlm",
        generation_id="gen-vlm",
        source_fingerprint="f" * 64,
        page_no=page_no,
        page_label=str(page_no),
        doc_item_ref=f"#/pictures/{media_id}",
        bbox=NormalizedMediaBBox(x0=0.1, y0=0.2, x1=0.7, y1=0.8),
        kind="diagram",
        section_node_id="sec-vlm",
        crop_version="omarag-media-crop-v1",
        pixel_sha256=digest,
        perceptual_hash="0" * 16,
        width_px=64,
        height_px=48,
        mime_type="image/webp",
        crop_resource=f"media/blobs/{digest}.webp",
        thumbnail_resource=f"media/thumbnails/{digest}.webp",
    )


def _structure(total_pages: int = 1) -> BookStructure:
    return BookStructure(
        logical_document_id="book-vlm",
        mode="body-headings",
        confidence=0.9,
        total_pages=total_pages,
        nodes=[
            BookStructureNode(
                node_id="sec-vlm",
                depth=0,
                ordinal=0,
                title="Hydratation",
                normalized_title="hydratation",
                page_start=1,
                page_end=total_pages,
                source_kind="body-heading",
                confidence=0.9,
            )
        ],
    )


@pytest.mark.asyncio
async def test_vlm_enrichment_never_sends_book_crops_to_remote_ollama(
    tmp_path: Path,
) -> None:
    asset = _materialized_asset(tmp_path)

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("remote Ollama must not receive inventory or image requests")

    async with httpx.AsyncClient(
        base_url="http://192.0.2.10:11434",
        transport=httpx.MockTransport(unexpected_request),
    ) as client:
        result = await enrich_media_assets_vlm(
            assets=[asset],
            workspace_root=tmp_path,
            llm_url="http://192.0.2.10:11434",
            model="remote-vl",
            client=client,
        )

    assert result.failure == "ollama-endpoint-not-local"
    assert result.assets == [asset]


@pytest.mark.asyncio
async def test_vlm_enrichment_pins_digest_and_only_adds_routing_text(tmp_path: Path) -> None:
    asset = _materialized_asset(tmp_path)
    calls: list[str] = []
    admissions = 0
    digest = "a" * 64

    @asynccontextmanager
    async def inference_guard():
        nonlocal admissions
        admissions += 1
        yield

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "local-vl:latest",
                            "digest": digest,
                            "capabilities": ["completion", "vision"],
                        }
                    ]
                },
            )
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload["model"] == "local-vl:latest"
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["format"]["additionalProperties"] is False
        assert payload["options"] == {
            "temperature": 0,
            "seed": 0,
            "num_ctx": 2048,
            "num_predict": 192,
        }
        assert len(payload["messages"]) == 1
        assert len(payload["messages"][0]["images"]) == 1
        return httpx.Response(
            200,
            json={
                "model": "local-vl:latest",
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "description": "Diagramm zur Hydratationswärme.",
                            "visible_terms": ["Zement", "Temperatur"],
                        }
                    ),
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await enrich_media_assets_vlm(
            assets=[asset],
            workspace_root=tmp_path,
            llm_url="http://127.0.0.1:11434",
            model="local-vl",
            expected_digest=digest,
            client=client,
            inference_guard=inference_guard,
        )

    assert calls == ["/api/tags", "/api/chat", "/api/tags"]
    assert admissions == 1
    assert "/api/pull" not in calls
    assert result.used is True
    assert result.enriched_count == 1
    assert result.failure is None
    derived = result.assets[0].derived_text[0]
    assert derived.origin == "model-derived"
    assert derived.model_digest == digest
    assert derived.evidence_id is None
    assert result.assets[0].evidence_ids == []
    snapshot = build_media_snapshot(
        structure=_structure(),
        evidence=[],
        assets=result.assets,
        terms=[
            KnowledgeTerm(
                term_id="term-heat",
                canonical="Hydratationswärme",
                normalized="hydratationswarme",
                kind="caption",
                confidence=0.8,
            )
        ],
    )
    routed = next(link for link in snapshot.links if link.target_id == "term-heat")
    assert routed.origin == "model-derived"
    assert routed.model_digest == digest
    assert routed.evidence_ids == []
    proposal = build_okf_media_proposal(
        result.assets[0],
        source_document_resource="/references/documents/book-vlm.md",
    )
    assert proposal.generated is not None
    assert proposal.generated.model_digest == digest
    assert proposal.description is not None


@pytest.mark.asyncio
async def test_vlm_rejects_catalog_digest_before_reading_or_sending_crop(
    tmp_path: Path,
) -> None:
    asset = _materialized_asset(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path != "/api/tags":
            raise AssertionError("a catalog mismatch must not transmit the crop")
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "local-vl",
                        "digest": "installed-digest",
                        "capabilities": ["vision"],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await enrich_media_assets_vlm(
            assets=[asset],
            workspace_root=tmp_path,
            llm_url="http://127.0.0.1:11434",
            model="local-vl",
            expected_digest="catalog-digest",
            client=client,
        )

    assert calls == ["/api/tags"]
    assert result.failure == "catalog-digest-mismatch"
    assert result.assets == [asset]


@pytest.mark.asyncio
async def test_vlm_missing_model_and_work_cap_degrade_without_pull(tmp_path: Path) -> None:
    first = _materialized_asset(tmp_path, media_id="media-first", color="navy")
    second = _materialized_asset(
        tmp_path,
        media_id="media-second",
        color="orange",
        page_no=2,
    )
    calls: list[str] = []

    def missing_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(missing_handler),
    ) as client:
        missing = await enrich_media_assets_vlm(
            assets=[first, second],
            workspace_root=tmp_path,
            llm_url="http://127.0.0.1:11434",
            model="not-installed",
            client=client,
        )

    assert calls == ["/api/tags"]
    assert missing.failure == "model-not-installed"
    assert missing.enriched_count == 0
    assert all(not item.derived_text for item in missing.assets)

    digest = "b" * 64
    calls.clear()

    def bounded_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "local-vl",
                            "digest": digest,
                            "capabilities": ["vision"],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "local-vl",
                "done": True,
                "message": {
                    "content": json.dumps(
                        {"description": "Sichtbares Diagramm.", "visible_terms": []}
                    )
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(bounded_handler),
    ) as client:
        bounded = await enrich_media_assets_vlm(
            assets=[first, second],
            workspace_root=tmp_path,
            llm_url="http://127.0.0.1:11434",
            model="local-vl",
            limits=MediaVlmLimits(max_crops=1),
            client=client,
        )

    assert calls == ["/api/tags", "/api/chat", "/api/tags"]
    assert bounded.enriched_count == 1
    assert bounded.truncated_count == 1
    assert bounded.failure is None


@pytest.mark.asyncio
async def test_vlm_rejects_non_blob_resource_before_network(tmp_path: Path) -> None:
    asset = _materialized_asset(tmp_path)
    unsafe = asset.model_copy(update={"crop_resource": asset.thumbnail_resource})
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await enrich_media_assets_vlm(
            assets=[unsafe],
            workspace_root=tmp_path,
            llm_url="http://127.0.0.1:11434",
            model="local-vl",
            client=client,
        )

    assert calls == []
    assert result.failure == "no-materialized-crops"
    assert result.assets == [unsafe]


@pytest.mark.asyncio
async def test_vlm_discards_description_if_installed_digest_changes(tmp_path: Path) -> None:
    asset = _materialized_asset(tmp_path)
    inventory_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inventory_calls
        if request.url.path == "/api/tags":
            inventory_calls += 1
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "local-vl",
                            "digest": ("a" if inventory_calls == 1 else "b") * 64,
                            "capabilities": ["vision"],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "local-vl",
                "done": True,
                "message": {
                    "content": json.dumps(
                        {"description": "Ein sichtbares Diagramm.", "visible_terms": []}
                    )
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await enrich_media_assets_vlm(
            assets=[asset],
            workspace_root=tmp_path,
            llm_url="http://127.0.0.1:11434",
            model="local-vl",
            client=client,
        )

    assert result.failure == "model-digest-changed"
    assert result.enriched_count == 0
    assert result.assets == [asset]
    assert result.assets[0].derived_text == []
