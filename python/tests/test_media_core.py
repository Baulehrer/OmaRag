from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from omarag_bridge.models.api import CreateWorkspaceRequest
from omarag_bridge.models.book import (
    BookKnowledgeSnapshot,
    BookRagGraph,
    BookStructure,
    BookStructureNode,
    EvidenceAnchor,
    EvidenceRecord,
    KnowledgeTerm,
)
from omarag_bridge.models.domain import Citation, JobStatus
from omarag_bridge.models.media import (
    MediaAsset,
    MediaEvidence,
    MediaText,
    NormalizedMediaBBox,
    PagePreviewEvidence,
    VisualEvidenceResponse,
)
from omarag_bridge.services.book_snapshot_service import build_book_knowledge_snapshot
from omarag_bridge.services.media_index import (
    DenseMediaRecord,
    LanceDbMediaDenseIndex,
    LocalMediaDenseIndex,
    select_visual_evidence,
)
from omarag_bridge.services.media_service import (
    build_media_snapshot,
    build_okf_media_proposal,
    collect_media_assets,
    mark_media_blob_references,
    materialize_collected_media,
    materialize_media_crops,
    materialize_selected_media_crops,
    normalize_media_bbox,
    stable_media_id,
    sweep_unreferenced_media_blobs,
    visual_evidence_blob_digests,
)
from omarag_bridge.services.visual_evidence_service import VisualEvidenceService
from omarag_bridge.services.workspace_service import WorkspaceService
from omarag_bridge.store import StateStore


def _structure() -> BookStructure:
    return BookStructure(
        logical_document_id="book-media",
        mode="body-headings",
        confidence=0.9,
        total_pages=1,
        nodes=[
            BookStructureNode(
                node_id="sec-media",
                depth=0,
                ordinal=0,
                title="Hydratation",
                normalized_title="hydratation",
                page_start=1,
                page_end=1,
                source_kind="body-heading",
                confidence=0.9,
            )
        ],
    )


def _evidence() -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="ev-media",
        raw_content="Das Diagramm zeigt den Wasserzementwert.",
        content_hash="content-media",
        anchors=[
            EvidenceAnchor(
                page_no=1,
                source_ref="#/pictures/1",
                bbox=(20.0, 160.0, 160.0, 20.0),
                label="picture",
            )
        ],
        page_start=1,
        page_end=1,
        section_node_id="sec-media",
        labels=["picture"],
    )


def _pdf(path: Path) -> str:
    image = Image.new("RGB", (200, 200), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((40, 40, 160, 160), fill="navy")
    drawing.line((40, 100, 160, 60), fill="orange", width=8)
    image.save(path, format="PDF", resolution=72)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asset(fingerprint: str, *, media_id: str = "media-one") -> MediaAsset:
    return MediaAsset(
        media_id=media_id,
        logical_document_id="book-media",
        generation_id="gen-media",
        source_fingerprint=fingerprint,
        page_no=1,
        page_label="1",
        doc_item_ref="#/pictures/1",
        bbox=NormalizedMediaBBox(x0=0.2, y0=0.2, x1=0.8, y1=0.8),
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
                evidence_id="ev-media",
                source_ref="#/pictures/1",
            )
        ],
        evidence_ids=["ev-media"],
    )


def test_bbox_identity_and_text_origins_are_strict() -> None:
    top_left = normalize_media_bbox(
        SimpleNamespace(l=20, t=20, r=160, b=160),
        page_width=200,
        page_height=200,
        coord_origin="TOPLEFT",
    )
    bottom_left = normalize_media_bbox(
        SimpleNamespace(l=20, t=180, r=160, b=40),
        page_width=200,
        page_height=200,
        coord_origin="BOTTOMLEFT",
    )
    assert top_left == NormalizedMediaBBox(x0=0.1, y0=0.1, x1=0.8, y1=0.8)
    assert (bottom_left.x0, bottom_left.y0, bottom_left.x1, bottom_left.y1) == pytest.approx(
        (0.1, 0.1, 0.8, 0.8)
    )
    assert stable_media_id("pdf-sha", 1, "#/pictures/1", top_left) == stable_media_id(
        "pdf-sha", 1, "#/pictures/1", top_left
    )
    with pytest.raises(ValidationError, match="pin the model digest"):
        MediaText(text="unverified", origin="model-derived")


def test_collect_crop_deduplicate_graph_and_okf_contract(tmp_path: Path) -> None:
    pdf = tmp_path / "media.pdf"
    fingerprint = _pdf(pdf)
    caption = SimpleNamespace(
        self_ref="#/texts/caption-1",
        label="caption",
        text="Wasserzementwert im Verlauf",
        prov=[],
    )
    picture = SimpleNamespace(
        self_ref="#/pictures/1",
        label="diagram",
        text="",
        captions=[SimpleNamespace(cref="#/texts/caption-1")],
        prov=[
            SimpleNamespace(
                page_no=1,
                bbox=SimpleNamespace(l=40, t=160, r=160, b=40, coord_origin="BOTTOMLEFT"),
            )
        ],
    )
    page_background = SimpleNamespace(
        self_ref="#/pictures/full-page-scan",
        label="picture",
        text="",
        captions=[],
        prov=[
            SimpleNamespace(
                page_no=1,
                bbox=SimpleNamespace(l=4, t=196, r=196, b=4, coord_origin="BOTTOMLEFT"),
            )
        ],
    )
    document = SimpleNamespace(
        pages={1: SimpleNamespace(size=SimpleNamespace(width=200, height=200))},
        iterate_items=lambda: iter(((picture, 0), (caption, 1), (page_background, 2))),
    )
    assets = collect_media_assets(
        document=document,
        source_pdf=pdf,
        source_fingerprint=fingerprint,
        logical_document_id="book-media",
        generation_id="gen-media",
        structure=_structure(),
        evidence=[_evidence()],
        page_labels={"1": 1},
    )
    assert len(assets) == 1
    assert assets[0].captions[0].origin == "native-caption"
    assert assets[0].nearby_text[0].evidence_id == "ev-media"

    duplicate = assets[0].model_copy(
        update={"media_id": "media-copy", "doc_item_ref": "#/pictures/copy"}
    )
    rendered = materialize_media_crops(
        pdf,
        [assets[0], duplicate],
        tmp_path,
        expected_fingerprint=fingerprint,
    )
    assert rendered[0].pixel_sha256 == rendered[1].pixel_sha256
    assert (tmp_path / rendered[0].crop_resource).is_file()
    assert (tmp_path / rendered[0].thumbnail_resource).is_file()
    assert rendered[0].width_px == rendered[0].height_px
    assert rendered[0].width_px >= 100

    graph = build_media_snapshot(
        structure=_structure(),
        evidence=[_evidence()],
        assets=rendered,
        terms=[
            KnowledgeTerm(
                term_id="term-water",
                canonical="Wasserzementwert",
                normalized="wasserzementwert",
                kind="caption",
                confidence=0.9,
            )
        ],
    )
    assert graph.duplicate_groups[0].match == "exact"
    assert {link.relation for link in graph.links} >= {
        "section_contains_media",
        "evidence_depicts_media",
        "media_mentions_term",
        "media_duplicate_of",
    }
    proposal = build_okf_media_proposal(
        rendered[0],
        source_document_resource="/references/documents/book-media.md",
        source_title="Baustoffkunde",
    )
    assert proposal.type == "Book Diagram"
    assert proposal.resource.startswith("/references/media/sha256/")
    assert proposal.generated is None


def test_metadata_only_and_bounded_materialization_preserve_existing_blobs(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "bounded.pdf"
    fingerprint = _pdf(pdf)
    first = _asset(fingerprint, media_id="media-first")
    second = _asset(fingerprint, media_id="media-second").model_copy(
        update={
            "doc_item_ref": "#/pictures/2",
            "bbox": NormalizedMediaBBox(x0=0.05, y0=0.05, x1=0.45, y1=0.45),
        }
    )

    metadata_root = tmp_path / "metadata-only"
    metadata_only = materialize_collected_media(
        source_pdf=pdf,
        assets=[first, second],
        workspace_root=metadata_root,
        expected_fingerprint=fingerprint,
        expected_generation_id="gen-media",
        materialize_limit=0,
    )
    assert all(asset.crop_resource is None for asset in metadata_only)
    assert not (metadata_root / "media").exists()

    bounded_root = tmp_path / "bounded"
    bounded = materialize_collected_media(
        source_pdf=pdf,
        assets=[first, second],
        workspace_root=bounded_root,
        expected_fingerprint=fingerprint,
        expected_generation_id="gen-media",
        materialize_limit=1,
    )
    assert sum(asset.crop_resource is not None for asset in bounded) == 1
    assert len(list((bounded_root / "media" / "blobs").glob("*.webp"))) == 1

    materialized = next(asset for asset in bounded if asset.crop_resource)
    pdf.unlink()
    assert materialize_selected_media_crops(
        pdf,
        [materialized],
        bounded_root,
        expected_fingerprint=fingerprint,
        expected_generation_id="gen-media",
    ) == [materialized]

    with pytest.raises(ValueError, match="another generation"):
        materialize_selected_media_crops(
            pdf,
            [first.model_copy(update={"generation_id": "stale-generation"})],
            tmp_path / "rejected",
            expected_fingerprint=fingerprint,
            expected_generation_id="gen-media",
        )
    assert not (tmp_path / "rejected" / "media").exists()


def test_media_blob_mark_and_sweep_is_dry_run_first_and_protects_references(
    tmp_path: Path,
) -> None:
    active_digest = "a" * 64
    orphan_digest = "b" * 64
    cached_run_digest = "c" * 64
    media_root = tmp_path / "database" / "media"
    for folder in ("blobs", "thumbnails"):
        directory = media_root / folder
        directory.mkdir(parents=True)
        (directory / f"{active_digest}.webp").write_bytes(b"active")
        (directory / f"{orphan_digest}.webp").write_bytes(b"orphan")
        (directory / f"{cached_run_digest}.webp").write_bytes(b"cached-run")
    unknown = media_root / "blobs" / "not-a-content-address.webp"
    unknown.write_bytes(b"keep")
    active = _asset("f" * 64).model_copy(
        update={
            "pixel_sha256": active_digest,
            "width_px": 10,
            "height_px": 10,
            "mime_type": "image/webp",
            "crop_resource": f"media/blobs/{active_digest}.webp",
            "thumbnail_resource": f"media/thumbnails/{active_digest}.webp",
        }
    )
    cached_visual = VisualEvidenceResponse(
        media=[
            MediaEvidence(
                media_id="media-from-completed-run",
                kind="diagram",
                thumbnail_url=(
                    f"/v1/workspaces/workspace-one/media/blobs/{cached_run_digest}/thumbnail"
                ),
                preview_url=(f"/v1/workspaces/workspace-one/media/blobs/{cached_run_digest}/crop"),
            )
        ]
    )
    assert visual_evidence_blob_digests([cached_visual]) == {cached_run_digest}
    marked = mark_media_blob_references([active], visual_evidence=[cached_visual])

    dry_run = sweep_unreferenced_media_blobs(
        tmp_path / "database",
        marked,
        dry_run=True,
        minimum_age_seconds=0,
        now=10**10,
    )
    assert dry_run.removed == ()
    assert {path.name for path in dry_run.candidates} == {f"{orphan_digest}.webp"}
    assert len(dry_run.candidates) == 2
    assert all(path.exists() for path in dry_run.candidates)
    assert unknown in dry_run.protected

    swept = sweep_unreferenced_media_blobs(
        tmp_path / "database",
        marked,
        dry_run=False,
        minimum_age_seconds=0,
        now=10**10,
    )
    assert len(swept.removed) == 2
    assert swept.reclaimed_bytes == len(b"orphan") * 2
    assert all(not path.exists() for path in swept.removed)
    assert (media_root / "blobs" / f"{active_digest}.webp").is_file()
    assert (media_root / "thumbnails" / f"{active_digest}.webp").is_file()
    assert (media_root / "blobs" / f"{cached_run_digest}.webp").is_file()
    assert (media_root / "thumbnails" / f"{cached_run_digest}.webp").is_file()
    assert unknown.is_file()


def test_store_enumerates_surviving_visual_evidence_for_gc(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Visual retention")
    )
    run = store.create_run(
        "run-visual-gc",
        workspace.id,
        {
            "session_id": "session-visual-gc",
            "question": "Zeige das Diagramm",
            "evidence_mode": "strict",
        },
    )
    store.update_run(run.id, status=JobStatus.COMPLETED)
    digest = "d" * 64
    response = VisualEvidenceResponse(
        media=[
            MediaEvidence(
                media_id="media-gc",
                kind="diagram",
                preview_url=(f"/v1/workspaces/{workspace.id}/media/blobs/{digest}/crop"),
            )
        ]
    )
    store.save_run_visual_evidence(run.id, response)

    persisted = store.run_visual_evidence(workspace.id)
    assert len(persisted) == 1
    assert visual_evidence_blob_digests(persisted) == {digest}
    store.close()


def test_visual_evidence_lazily_materializes_only_its_selected_crop_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "managed.pdf"
    fingerprint = _pdf(source)
    anchors = [
        EvidenceAnchor(
            page_no=1,
            source_ref=f"#/pictures/{index}",
            bbox=(10.0 * index, 150.0, 50.0 + 10.0 * index, 50.0),
            label="picture",
        )
        for index in range(5)
    ]
    evidence = EvidenceRecord(
        evidence_id="ev-lazy-media",
        raw_content="Die Abbildungen zeigen den Wasserzementwert.",
        content_hash="content-lazy-media",
        anchors=anchors,
        page_start=1,
        page_end=1,
        section_node_id="sec-media",
        labels=["picture"],
    )
    assets = [
        _asset(fingerprint, media_id=f"media-lazy-{index}").model_copy(
            update={
                "doc_item_ref": f"#/pictures/{index}",
                "bbox": NormalizedMediaBBox(
                    x0=0.02 + index * 0.08,
                    y0=0.05 + index * 0.03,
                    x1=0.42 + index * 0.08,
                    y1=0.55 + index * 0.03,
                ),
                "evidence_ids": [evidence.evidence_id],
                "nearby_text": [
                    MediaText(
                        text=evidence.raw_content,
                        origin="nearby-text",
                        source_ref=f"#/pictures/{index}",
                        evidence_id=evidence.evidence_id,
                    )
                ],
            }
        )
        for index in range(5)
    ]
    media = build_media_snapshot(
        structure=_structure(),
        evidence=[evidence],
        assets=assets,
    )
    snapshot = build_book_knowledge_snapshot(
        logical_document_id="book-media",
        generation_id="gen-media",
        fingerprint=fingerprint,
        config_hash="config-lazy-media",
        structure=_structure(),
        evidence=[evidence],
        graph=BookRagGraph(),
        media=media,
    )
    store = StateStore(tmp_path / "state.sqlite3")
    workspaces = WorkspaceService(tmp_path / "workspaces", store)
    workspace = workspaces.create(CreateWorkspaceRequest(name="Lazy Media"))
    store.upsert_document(
        workspace.id,
        str(source),
        fingerprint,
        {
            "logical_document_id": "book-media",
            "generation_id": "gen-media",
            "original_source": str(source),
            "managed_source": str(source),
            "pipeline_version": "book-v2-test",
            "book_metadata": {"title": "Baustoffkunde"},
            "quality": {},
            "book_knowledge_snapshot": snapshot.model_dump(mode="json"),
        },
    )
    store.create_run(
        "run-lazy-media",
        workspace.id,
        {
            "session_id": "session-lazy-media",
            "question": "Zeige die Abbildungen zum Wasserzementwert",
            "evidence_mode": "normal",
        },
    )
    store.update_run(
        "run-lazy-media",
        status=JobStatus.COMPLETED,
        answer="Die Abbildungen befinden sich auf Seite 1.",
        citations=[
            Citation(
                evidence_id=evidence.evidence_id,
                generation_id="gen-media",
                chunk_id="chunk-lazy-media",
                logical_document_id="book-media",
                document_title="Baustoffkunde",
                pages=[1],
                excerpt=evidence.raw_content,
            )
        ],
    )

    service = VisualEvidenceService(store, workspaces)

    def temporarily_unavailable(*_args: object, **_kwargs: object) -> list[MediaAsset]:
        raise FileNotFoundError("managed source is temporarily unavailable")

    # A source/sidecar race may legitimately leave only page evidence for one
    # request. It must not poison the persisted run cache.
    with monkeypatch.context() as temporary_patch:
        temporary_patch.setattr(
            "omarag_bridge.services.visual_evidence_service.materialize_selected_media_crops",
            temporarily_unavailable,
        )
        transient = service.get_or_build("run-lazy-media")
    assert transient.pages
    assert transient.media == []
    assert store.get_run_visual_evidence("run-lazy-media") is None

    # A generation change detected by the final compare-and-set check keeps an
    # otherwise valid old-generation response out of the persistent cache.
    stable_pin = service._document_pin(workspace.id, "book-media")
    assert stable_pin is not None
    changed_pin = stable_pin.__class__(
        generation_id="gen-raced-reindex",
        source_fingerprint=stable_pin.source_fingerprint,
        managed_source=stable_pin.managed_source,
    )
    observed_pins = iter((stable_pin, changed_pin))
    with monkeypatch.context() as race_patch:
        race_patch.setattr(service, "_document_pin", lambda *_args: next(observed_pins))
        raced = service.get_or_build("run-lazy-media")
    assert raced.media
    assert all(item.media_id.startswith("media-lazy-") for item in raced.media)
    assert store.get_run_visual_evidence("run-lazy-media") is None

    response = service.get_or_build("run-lazy-media")
    blob_root = Path(workspace.path) / "database" / "media" / "blobs"
    assert 1 <= len(response.media) <= 4
    assert len(list(blob_root.glob("*.webp"))) <= 4
    assert all("/media/blobs/" in str(item.preview_url) for item in response.media)
    assert all(
        store.book_media_asset(workspace.id, asset.media_id)["crop_resource"] is None
        for asset in assets
    )
    assert service.get_or_build("run-lazy-media") == response

    # A later reindex replaces the active sidecar, but the completed answer
    # keeps its exact old selection via immutable content-addressed blob URLs.
    next_generation = "gen-media-next"
    next_media = build_media_snapshot(
        structure=_structure(),
        evidence=[evidence],
        assets=[asset.model_copy(update={"generation_id": next_generation}) for asset in assets],
    )
    next_snapshot = build_book_knowledge_snapshot(
        logical_document_id="book-media",
        generation_id=next_generation,
        fingerprint=fingerprint,
        config_hash="config-lazy-media-next",
        structure=_structure(),
        evidence=[evidence],
        graph=BookRagGraph(),
        media=next_media,
    )
    store.upsert_document(
        workspace.id,
        str(source),
        fingerprint,
        {
            "logical_document_id": "book-media",
            "generation_id": next_generation,
            "original_source": str(source),
            "managed_source": str(source),
            "pipeline_version": "book-v3-test",
            "book_metadata": {"title": "Baustoffkunde"},
            "quality": {},
            "book_knowledge_snapshot": next_snapshot.model_dump(mode="json"),
        },
    )
    restored = service.get_or_build("run-lazy-media")
    assert restored == response
    digest = str(restored.media[0].preview_url).rsplit("/", 2)[-2]
    assert service.blob_path(workspace.id, digest, thumbnail=False).is_file()
    store.close()


def test_v2_snapshot_loads_and_media_v3_store_dense_and_visual_round_trip(
    tmp_path: Path,
) -> None:
    legacy = BookKnowledgeSnapshot.model_validate(
        {
            "schema_version": "2",
            "logical_document_id": "book-media",
            "generation_id": "gen-media",
            "fingerprint": "pdf-sha",
            "config_hash": "config-sha",
            "content_hash": "legacy-hash",
            "structure": _structure().model_dump(mode="json"),
            "evidence": [],
            "graph": {},
        }
    )
    assert legacy.schema_version == "2"
    assert legacy.media.assets == []

    asset = _asset("pdf-sha").model_copy(
        update={
            "pixel_sha256": "a" * 64,
            "perceptual_hash": "0" * 16,
            "width_px": 640,
            "height_px": 480,
            "mime_type": "image/webp",
            "crop_resource": "media/blobs/a.webp",
            "thumbnail_resource": "media/thumbnails/a.webp",
        }
    )
    media = build_media_snapshot(structure=_structure(), evidence=[_evidence()], assets=[asset])
    snapshot = build_book_knowledge_snapshot(
        logical_document_id="book-media",
        generation_id="gen-media",
        fingerprint="pdf-sha",
        config_hash="config-sha",
        structure=_structure(),
        evidence=[_evidence()],
        graph=BookRagGraph(),
        media=media,
    )
    assert snapshot.schema_version == "3"

    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Media")
    )
    store.save_book_knowledge_snapshot(workspace.id, snapshot)
    assert store.book_media_asset(workspace.id, asset.media_id)["kind"] == "diagram"
    media_results = store.search_book_media(workspace.id, "Wasserzementwert")
    assert media_results[0]["media_id"] == asset.media_id
    assert store.search_book_media(workspace.id, "Was ist das und wie ist es?") == []

    dense = LocalMediaDenseIndex(tmp_path / "media-index" / "vectors.sqlite3")
    dense.rebuild(
        [
            DenseMediaRecord(
                media_id=asset.media_id,
                logical_document_id=asset.logical_document_id,
                page_no=1,
                vector=[1.0, 0.0],
            )
        ],
        generation_id="visual-gen-1",
        model_digest="sha256:model",
    )
    assert dense.search([0.9, 0.1], limit=4)[0].media_id == asset.media_id
    dense.close()

    pytest.importorskip("lancedb")
    ann = LanceDbMediaDenseIndex(tmp_path / "media-index" / "vectors.lancedb")
    ann.rebuild(
        [
            DenseMediaRecord(
                media_id=asset.media_id,
                logical_document_id=asset.logical_document_id,
                page_no=1,
                vector=[1.0, 0.0],
            ),
            DenseMediaRecord(
                media_id="media-foreign",
                logical_document_id="book-foreign",
                page_no=2,
                vector=[0.0, 1.0],
            ),
        ],
        generation_id="visual-ann-1",
        model_digest="sha256:model",
    )
    ann_hits = ann.search([0.9, 0.1], limit=4, logical_document_ids=[asset.logical_document_id])
    assert [hit.media_id for hit in ann_hits] == [asset.media_id]
    assert ann.active_generation()["generation_id"] == "visual-ann-1"
    ann.close()

    run = store.create_run(
        "run-media",
        workspace.id,
        {
            "session_id": "session-media",
            "question": "Zeige das Diagramm",
            "evidence_mode": "normal",
        },
    )
    response = select_visual_evidence(
        query="Wasserzementwert",
        pages=[
            PagePreviewEvidence(
                page_id="page-book-media-1",
                citation_index=0,
                document_id="book-media",
                document_title="Baustoffkunde",
                page=1,
                preview_url="/v1/pages/book-media/1",
            )
        ],
        assets={asset.media_id: asset},
        rankings={"caption-fts": [(asset.media_id, 0.9)]},
        media_urls={
            asset.media_id: (
                f"/v1/media/{asset.media_id}/thumbnail",
                f"/v1/media/{asset.media_id}/crop",
            )
        },
        document_titles={"book-media": "Baustoffkunde"},
        chunk_ids_by_evidence={"ev-media": "chunk-media"},
    )
    assert response.schema_version == 1
    assert len(response.media) == 1
    wire = response.model_dump(mode="json")
    assert set(wire) == {"schema_version", "pages", "media", "selection"}
    assert set(wire["pages"][0]) == {
        "page_id",
        "citation_index",
        "document_id",
        "document_title",
        "page",
        "score",
        "primary_anchors",
        "context_anchors",
        "preview_url",
    }
    assert set(wire["media"][0]) == {
        "media_id",
        "kind",
        "document_id",
        "document_title",
        "page",
        "bbox",
        "caption",
        "caption_origin",
        "score",
        "evidence_ids",
        "chunk_ids",
        "thumbnail_url",
        "preview_url",
        "width",
        "height",
    }
    assert set(wire["selection"]) == {"max_media", "cut_reason"}
    store.save_run_visual_evidence(run.id, response)
    restored = store.get_run_visual_evidence(run.id)
    assert restored is not None
    assert restored["pages"][0]["page"] == 1
    assert restored["media"][0]["media_id"] == asset.media_id
    assert restored["media"][0]["chunk_ids"] == ["chunk-media"]
    store.close()
