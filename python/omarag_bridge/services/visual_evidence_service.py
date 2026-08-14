from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote

from ..models.domain import JobStatus, RunSnapshot
from ..models.errors import ConflictError
from ..models.media import (
    MediaAsset,
    PageEvidence,
    VisualEvidenceResponse,
)
from ..store import StateStore
from .media_index import select_visual_evidence
from .media_service import (
    build_okf_media_proposal,
    materialize_selected_media_crops,
    media_asset_has_materialized_blobs,
    resolve_media_resource,
)
from .workspace_service import WorkspaceService


def _asset(payload: dict[str, object]) -> MediaAsset:
    """Discard query-only FTS columns before strict model validation."""

    return MediaAsset.model_validate(
        {key: payload[key] for key in MediaAsset.model_fields if key in payload}
    )


@dataclass(frozen=True)
class _DocumentGenerationPin:
    generation_id: str
    source_fingerprint: str
    managed_source: str


@dataclass(frozen=True)
class _VisualBuild:
    response: VisualEvidenceResponse
    cacheable: bool
    document_pins: dict[str, _DocumentGenerationPin]


class VisualEvidenceService:
    """Compose cited pages and real book-media crops without delaying answers.

    The response is built lazily after a run has completed. It uses only the
    current, generation-bound media sidecar and remains useful without a visual
    embedding model: exact evidence links, cited pages and caption FTS are
    fused deterministically.
    """

    def __init__(self, store: StateStore, workspaces: WorkspaceService) -> None:
        self.store = store
        self.workspaces = workspaces
        # API requests and the eager post-answer builder may race for the same
        # run. Serializing this tiny, bounded path avoids duplicate crops and a
        # last-writer-wins cache decision. Heavy-work admission remains owned
        # by ResourceCoordinator at the two public call sites.
        self._build_lock = RLock()

    def get_or_build(self, run_id: str) -> VisualEvidenceResponse:
        with self._build_lock:
            run = self.store.get_run(run_id)
            if run.status is not JobStatus.COMPLETED:
                raise ConflictError(
                    "Visual evidence is available only after the answer has completed",
                    details={"run_id": run_id, "status": run.status.value},
                )
            cached = self.store.get_run_visual_evidence(run_id)
            if cached is not None:
                return VisualEvidenceResponse.model_validate(cached)

            run_pin = self._run_pin(run)
            build = self._build(run)
            current_run = self.store.get_run(run_id)
            run_is_unchanged = (
                current_run.status is JobStatus.COMPLETED and self._run_pin(current_run) == run_pin
            )
            generations_are_unchanged = all(
                self._document_pin(run.workspace_id, logical_document_id) == pin
                for logical_document_id, pin in build.document_pins.items()
            )
            # Never make a transient page-only result permanent. Exact media
            # results remain useful through immutable blob URLs, but the cache
            # is committed only while both the Run and active book generations
            # still match the snapshots used by this build.
            if build.cacheable and run_is_unchanged and generations_are_unchanged:
                self.store.save_run_visual_evidence(run_id, build.response)
            elif not run_is_unchanged:
                raise ConflictError(
                    "Run changed while visual evidence was being selected",
                    details={"run_id": run_id},
                )
            return build.response

    def _build(self, run: RunSnapshot) -> _VisualBuild:
        manifest = self.workspaces.get(run.workspace_id)
        media_root = Path(manifest.path) / "database"
        pages = self._pages(run)
        assets: dict[str, MediaAsset] = {}
        evidence_ranking: list[tuple[str, float]] = []
        page_ranking: list[tuple[str, float]] = []
        caption_ranking: list[tuple[str, float]] = []
        titles: dict[str, str] = {}
        chunks_by_evidence: dict[str, str] = {}
        strong_media_ids: set[str] = set()
        weak_paths: dict[str, set[str]] = {}
        generation_sets: dict[str, set[str]] = {}
        for citation in run.citations:
            logical_id = citation.logical_document_id or citation.document_id
            generation = str(citation.generation_id or "")
            if logical_id and generation:
                generation_sets.setdefault(logical_id, set()).add(generation)
        ambiguous_documents = {
            logical_id
            for logical_id, generations in generation_sets.items()
            if len(generations) != 1
        }
        expected_generation_by_document = {
            logical_id: next(iter(generations))
            for logical_id, generations in generation_sets.items()
            if len(generations) == 1
        }
        document_pins: dict[str, _DocumentGenerationPin] = {}
        eligible_documents: set[str] = set()
        cacheable = not ambiguous_documents
        for logical_id, expected_generation in expected_generation_by_document.items():
            pin = self._document_pin(run.workspace_id, logical_id)
            if pin is None or pin.generation_id != expected_generation:
                # The citation refers to an older/newer generation than the
                # active sidecar. Page previews remain valid, but an empty crop
                # strip must be retryable rather than cached forever.
                cacheable = False
                continue
            document_pins[logical_id] = pin
            eligible_documents.add(logical_id)

        def eligible_generation(item: MediaAsset, generation: str | None) -> bool:
            # A media-bearing book-v2 citation must resolve to the exact index
            # generation that produced the answer. Missing/stale manifests
            # degrade to page previews instead of borrowing newer media.
            return bool(generation and item.generation_id == generation)

        for citation_index, citation in enumerate(run.citations):
            logical_id = citation.logical_document_id or citation.document_id
            if logical_id and citation.document_title:
                titles[logical_id] = citation.document_title
            if citation.evidence_id:
                chunks_by_evidence[citation.evidence_id] = citation.chunk_id
            if not logical_id or logical_id not in eligible_documents:
                continue
            generation = str(citation.generation_id or "") or None
            if generation != expected_generation_by_document.get(logical_id):
                continue
            for page in citation.pages:
                try:
                    page_assets = self.store.book_media_assets(
                        run.workspace_id,
                        logical_id,
                        page_no=page,
                        limit=32,
                    )
                except Exception:
                    cacheable = False
                    page_assets = []
                for payload in page_assets:
                    try:
                        item = _asset(payload)
                    except (TypeError, ValueError):
                        cacheable = False
                        continue
                    if not eligible_generation(item, generation):
                        continue
                    assets[item.media_id] = item
                    page_ranking.append((item.media_id, 1.0 / (citation_index + 1)))
                    weak_paths.setdefault(item.media_id, set()).add("cited-page")

            if not citation.evidence_id:
                continue
            try:
                links = self.store.book_media_links(
                    run.workspace_id,
                    node_id=citation.evidence_id,
                    limit=32,
                )
            except Exception:
                cacheable = False
                links = []
            for link in links:
                if link.get("source_id") != citation.evidence_id or link.get("relation") not in {
                    "evidence_depicts_media",
                    "evidence_context_for_media",
                }:
                    continue
                media_id = str(link.get("target_id") or "")
                if not media_id:
                    continue
                try:
                    item = _asset(self.store.book_media_asset(run.workspace_id, media_id))
                except Exception:
                    cacheable = False
                    continue
                if not eligible_generation(item, generation):
                    continue
                assets[item.media_id] = item
                relation = str(link.get("relation") or "")
                if relation == "evidence_depicts_media":
                    strong_media_ids.add(item.media_id)
                    evidence_ranking.append((item.media_id, 1.0 / (citation_index + 1)))
                else:
                    weak_paths.setdefault(item.media_id, set()).add("evidence-context")

        lexical: list[dict[str, object]] = []
        cited_pages_by_document: dict[str, set[int]] = {}
        for citation in run.citations:
            logical_id = citation.logical_document_id or citation.document_id
            if (
                logical_id
                and logical_id in eligible_documents
                and str(citation.generation_id or "") == expected_generation_by_document[logical_id]
            ):
                cited_pages_by_document.setdefault(logical_id, set()).update(citation.pages)
        for logical_id in sorted(cited_pages_by_document):
            try:
                lexical.extend(
                    self.store.search_book_media(
                        run.workspace_id,
                        run.question,
                        logical_document_id=logical_id,
                        limit=16,
                    )
                )
            except Exception:
                cacheable = False
                continue
        for rank, payload in enumerate(lexical, start=1):
            try:
                item = _asset(payload)
            except (TypeError, ValueError):
                cacheable = False
                continue
            expected = expected_generation_by_document.get(item.logical_document_id)
            cited_pages = cited_pages_by_document.get(item.logical_document_id, set())
            if not eligible_generation(item, expected) or not any(
                abs(item.page_no - page) <= 1 for page in cited_pages
            ):
                continue
            assets[item.media_id] = item
            caption_ranking.append((item.media_id, 1.0 / rank))
            weak_paths.setdefault(item.media_id, set()).add("caption-fts")

        eligible_media_ids = strong_media_ids | {
            media_id
            for media_id, paths in weak_paths.items()
            if "caption-fts" in paths and len(paths) >= 2
        }
        assets = {
            media_id: item for media_id, item in assets.items() if media_id in eligible_media_ids
        }

        for logical_id in sorted(eligible_documents):
            try:
                record = self.store.book_record(run.workspace_id, logical_id)
            except Exception:
                cacheable = False
                continue
            metadata = record.get("metadata") or {}
            title = str(metadata.get("title") or "")
            if title:
                titles.setdefault(logical_id, title)

        rankings = {
            "evidence-link": self._deduplicate_ranking(evidence_ranking),
            "cited-page": self._deduplicate_ranking(page_ranking),
            "caption-fts": self._deduplicate_ranking(caption_ranking),
        }
        rankings = {path: values for path, values in rankings.items() if values}
        rankings = {
            path: [(media_id, score) for media_id, score in values if media_id in assets]
            for path, values in rankings.items()
        }
        rankings = {path: values for path, values in rankings.items() if values}

        # Rank metadata first. Deliberate placeholders make unrendered BBox/
        # caption candidates selectable without publishing a URL for them.
        selection_assets = {
            media_id: self._selection_candidate(item) for media_id, item in assets.items()
        }
        preselection = select_visual_evidence(
            query=run.question,
            pages=(),
            assets=selection_assets,
            rankings=rankings,
            limit=4,
            min_fused_score=0.0,
            weights={"evidence-link": 2.0, "cited-page": 1.25, "caption-fts": 1.0},
        )
        assets, materialization_stable = self._materialize_selected(
            workspace_id=run.workspace_id,
            media_root=media_root,
            assets=assets,
            selected_media_ids=[item.media_id for item in preselection.media],
            expected_generation_by_document=expected_generation_by_document,
            document_pins=document_pins,
        )
        cacheable = cacheable and materialization_stable
        assets = {
            media_id: item
            for media_id, item in assets.items()
            if media_asset_has_materialized_blobs(media_root, item)
        }
        media_urls = {
            media_id: (
                f"/v1/workspaces/{quote(run.workspace_id, safe='')}/media/blobs/"
                f"{item.pixel_sha256}/thumbnail",
                f"/v1/workspaces/{quote(run.workspace_id, safe='')}/media/blobs/"
                f"{item.pixel_sha256}/crop",
            )
            for media_id, item in assets.items()
            if item.pixel_sha256
        }
        return _VisualBuild(
            response=select_visual_evidence(
                query=run.question,
                pages=pages,
                assets=assets,
                rankings=rankings,
                limit=4,
                min_fused_score=0.0,
                weights={"evidence-link": 2.0, "cited-page": 1.25, "caption-fts": 1.0},
                media_urls=media_urls,
                document_titles=titles,
                chunk_ids_by_evidence=chunks_by_evidence,
            ),
            cacheable=cacheable,
            document_pins=document_pins,
        )

    @staticmethod
    def _selection_candidate(asset: MediaAsset) -> MediaAsset:
        if asset.crop_resource and asset.thumbnail_resource:
            return asset
        # ``select_visual_evidence`` intentionally rejects non-rendered assets.
        # These values exist only for its bounded first pass and are never
        # serialized, persisted or exposed as routes.
        return asset.model_copy(
            update={
                "crop_resource": f"media/lazy/{asset.media_id}.webp",
                "thumbnail_resource": f"media/lazy/{asset.media_id}.thumb.webp",
                "quality_flags": [
                    flag for flag in asset.quality_flags if flag != "crop-unavailable"
                ],
            }
        )

    def _materialize_selected(
        self,
        *,
        workspace_id: str,
        media_root: Path,
        assets: dict[str, MediaAsset],
        selected_media_ids: list[str],
        expected_generation_by_document: dict[str, str],
        document_pins: dict[str, _DocumentGenerationPin],
    ) -> tuple[dict[str, MediaAsset], bool]:
        """Render only the at-most-four metadata candidates selected for this run."""

        selected = [assets[media_id] for media_id in selected_media_ids[:4] if media_id in assets]
        by_document: dict[str, list[MediaAsset]] = {}
        for asset in selected:
            by_document.setdefault(asset.logical_document_id, []).append(asset)
        updated: dict[str, MediaAsset] = {}
        stable = True
        for logical_document_id, document_assets in by_document.items():
            expected_generation = expected_generation_by_document.get(logical_document_id)
            expected_pin = document_pins.get(logical_document_id)
            if not expected_generation or expected_pin is None:
                stable = False
                continue
            try:
                record = self.store.book_record(workspace_id, logical_document_id)
            except Exception:
                stable = False
                continue
            current_pin = self._pin_from_record(record)
            record_generation = current_pin.generation_id
            record_fingerprint = current_pin.source_fingerprint
            if (
                current_pin != expected_pin
                or record_generation != expected_generation
                or any(asset.generation_id != expected_generation for asset in document_assets)
                or any(asset.source_fingerprint != record_fingerprint for asset in document_assets)
            ):
                stable = False
                continue
            source_value = current_pin.managed_source
            source = Path(source_value) if source_value else media_root / ".missing-source.pdf"
            try:
                rendered = materialize_selected_media_crops(
                    source,
                    document_assets,
                    media_root,
                    expected_fingerprint=record_fingerprint,
                    expected_generation_id=expected_generation,
                    max_assets=4,
                )
            except (FileNotFoundError, OSError, ValueError):
                # Visual evidence is optional. Page evidence remains available,
                # and valid old blobs from this exact generation remain useful.
                updated.update(
                    (asset.media_id, asset)
                    for asset in document_assets
                    if media_asset_has_materialized_blobs(media_root, asset)
                )
                if any(
                    not media_asset_has_materialized_blobs(media_root, asset)
                    for asset in document_assets
                ):
                    stable = False
                continue
            updated.update((asset.media_id, asset) for asset in rendered)
        if len(updated) != len(selected):
            stable = False
        return updated, stable

    @staticmethod
    def _pin_from_record(record: dict[str, Any]) -> _DocumentGenerationPin:
        return _DocumentGenerationPin(
            generation_id=str(record.get("generation_id") or ""),
            source_fingerprint=str(record.get("fingerprint") or ""),
            managed_source=str(record.get("managed_source") or ""),
        )

    def _document_pin(
        self, workspace_id: str, logical_document_id: str
    ) -> _DocumentGenerationPin | None:
        try:
            return self._pin_from_record(self.store.book_record(workspace_id, logical_document_id))
        except Exception:
            return None

    @staticmethod
    def _run_pin(run: RunSnapshot) -> tuple[object, ...]:
        """Fields that bind a visual result to the immutable answer evidence."""

        return (
            run.workspace_id,
            run.question,
            tuple(
                (
                    citation.evidence_id,
                    citation.chunk_id,
                    citation.logical_document_id,
                    citation.document_id,
                    citation.generation_id,
                    tuple(citation.pages),
                    tuple(citation.picture_refs),
                )
                for citation in run.citations
            ),
        )

    def _pages(self, run: RunSnapshot) -> list[PageEvidence]:
        pages: list[PageEvidence] = []
        seen: set[tuple[str, int]] = set()
        for citation_index, citation in enumerate(run.citations):
            document_id = citation.logical_document_id or citation.document_id
            for page in citation.pages:
                key = (document_id or citation.chunk_id, page)
                if key in seen:
                    continue
                seen.add(key)
                score = citation.relevance_score
                if score is None:
                    score = citation.rerank_score
                if score is None:
                    score = 1.0 / (citation_index + 1)
                pages.append(
                    PageEvidence(
                        page_id=f"page-{quote(key[0], safe='')}-{page}",
                        citation_index=citation_index,
                        document_id=document_id,
                        document_title=citation.document_title,
                        page=page,
                        score=score,
                        primary_anchors=[
                            anchor for anchor in citation.primary_anchors if anchor.page == page
                        ],
                        context_anchors=[
                            anchor for anchor in citation.context_anchors if anchor.page == page
                        ],
                        preview_url=(
                            f"/v1/workspaces/{quote(run.workspace_id, safe='')}/runs/"
                            f"{quote(run.id, safe='')}/citations/{citation_index}/preview"
                            "?max_px=1400"
                        ),
                    )
                )
                # Pages and media are deliberately separate UI concepts. Keep
                # enough cited pages for comparisons and multi-part answers;
                # only the crop strip is capped at four assets.
                if len(pages) == 12:
                    return pages
        return pages

    @staticmethod
    def _deduplicate_ranking(values: list[tuple[str, float]]) -> list[tuple[str, float]]:
        seen: set[str] = set()
        output: list[tuple[str, float]] = []
        for media_id, score in values:
            if media_id in seen:
                continue
            seen.add(media_id)
            output.append((media_id, score))
        return output

    def asset_path(self, workspace_id: str, media_id: str, *, thumbnail: bool) -> Path:
        manifest = self.workspaces.get(workspace_id)
        asset = _asset(self.store.book_media_asset(workspace_id, media_id))
        resource = asset.thumbnail_resource if thumbnail else asset.crop_resource
        if not resource:
            raise FileNotFoundError(f"Media asset {media_id} has no rendered crop")
        return resolve_media_resource(Path(manifest.path) / "database", resource)

    def blob_path(self, workspace_id: str, pixel_sha256: str, *, thumbnail: bool) -> Path:
        if len(pixel_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in pixel_sha256.casefold()
        ):
            raise ValueError("Media blob id must be a SHA-256 digest")
        manifest = self.workspaces.get(workspace_id)
        folder = "thumbnails" if thumbnail else "blobs"
        resource = f"media/{folder}/{pixel_sha256.casefold()}.webp"
        return resolve_media_resource(Path(manifest.path) / "database", resource)

    def okf_proposal(self, workspace_id: str, media_id: str) -> dict[str, object]:
        asset = _asset(self.store.book_media_asset(workspace_id, media_id))
        record = self.store.book_record(workspace_id, asset.logical_document_id)
        metadata = record.get("metadata") or {}
        proposal = build_okf_media_proposal(
            asset,
            source_document_resource=(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/documents/"
                f"{quote(asset.logical_document_id, safe='')}/knowledge-snapshot"
            ),
            source_title=str(metadata.get("title") or "") or None,
        )
        return proposal.model_dump(mode="json")
