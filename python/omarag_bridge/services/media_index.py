from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import struct
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import Field

from ..models.domain import Citation, StrictModel
from ..models.media import (
    MediaAsset,
    PageEvidence,
    PagePreviewEvidence,
    VisualEvidence,
    VisualEvidenceResponse,
    VisualEvidenceSelection,
)


class DenseMediaRecord(StrictModel):
    media_id: str
    logical_document_id: str
    page_no: int = Field(ge=1)
    vector: list[float] = Field(min_length=1)


class DenseMediaHit(StrictModel):
    media_id: str
    logical_document_id: str
    page_no: int = Field(ge=1)
    score: float
    rank: int = Field(ge=1)


class MediaDenseIndex(Protocol):
    """Rebuildable visual side-index, deliberately independent from Haiku."""

    def rebuild(
        self,
        records: Sequence[DenseMediaRecord],
        *,
        generation_id: str,
        model_digest: str,
    ) -> None: ...

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        logical_document_ids: Sequence[str] = (),
    ) -> list[DenseMediaHit]: ...

    def active_generation(self) -> dict[str, str | int] | None: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...


def _normalize(vector: Sequence[float]) -> list[float]:
    if not vector or len(vector) > 65_536:
        raise ValueError("Dense media vectors must contain between 1 and 65536 values")
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Dense media vectors must contain only finite values")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        raise ValueError("Dense media vectors must have a positive norm")
    return [value / norm for value in values]


def _encode(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _dot_blob(query: Sequence[float], payload: bytes) -> float:
    if len(payload) != len(query) * 4:
        raise ValueError("Stored media vector has an invalid dimension")
    values = struct.unpack(f"<{len(query)}f", payload)
    return sum(left * right for left, right in zip(query, values, strict=True))


class LocalMediaDenseIndex:
    """Small, fast local baseline with atomic generation swaps.

    It lives in its own SQLite file and never opens or writes Haiku's LanceDB.
    It is the dependency-free fallback for installations without LanceDB.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS media_dense_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                generation_id TEXT NOT NULL,
                model_digest TEXT NOT NULL,
                dimensions INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_dense_vectors (
                generation_id TEXT NOT NULL,
                media_id TEXT NOT NULL,
                logical_document_id TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                vector BLOB NOT NULL,
                PRIMARY KEY(generation_id, media_id)
            );
            CREATE INDEX IF NOT EXISTS media_dense_document_idx
                ON media_dense_vectors(generation_id, logical_document_id);
            """
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def rebuild(
        self,
        records: Sequence[DenseMediaRecord],
        *,
        generation_id: str,
        model_digest: str,
    ) -> None:
        if not generation_id or not model_digest:
            raise ValueError("Dense media generations require ids and a pinned model digest")
        dimensions = len(records[0].vector) if records else 0
        if any(len(record.vector) != dimensions for record in records):
            raise ValueError("All dense media vectors in a generation must have one dimension")
        normalized = [(record, _normalize(record.vector)) for record in records]
        if len({record.media_id for record, _vector in normalized}) != len(normalized):
            raise ValueError("Dense media generation contains duplicate media ids")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "DELETE FROM media_dense_vectors WHERE generation_id = ?",
                    (generation_id,),
                )
                self._db.executemany(
                    """INSERT INTO media_dense_vectors(
                           generation_id, media_id, logical_document_id, page_no, vector
                       ) VALUES (?, ?, ?, ?, ?)""",
                    [
                        (
                            generation_id,
                            record.media_id,
                            record.logical_document_id,
                            record.page_no,
                            _encode(vector),
                        )
                        for record, vector in normalized
                    ],
                )
                self._db.execute(
                    """INSERT INTO media_dense_meta(
                           singleton, generation_id, model_digest, dimensions
                       ) VALUES (1, ?, ?, ?)
                       ON CONFLICT(singleton) DO UPDATE SET
                           generation_id=excluded.generation_id,
                           model_digest=excluded.model_digest,
                           dimensions=excluded.dimensions""",
                    (generation_id, model_digest, dimensions),
                )
                self._db.execute(
                    "DELETE FROM media_dense_vectors WHERE generation_id != ?",
                    (generation_id,),
                )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def active_generation(self) -> dict[str, str | int] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM media_dense_meta WHERE singleton = 1").fetchone()
        return dict(row) if row is not None else None

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        logical_document_ids: Sequence[str] = (),
    ) -> list[DenseMediaHit]:
        if limit < 1 or limit > 1000:
            raise ValueError("Dense media search limit must be between 1 and 1000")
        query = _normalize(vector)
        active = self.active_generation()
        if active is None:
            return []
        if int(active["dimensions"]) == 0:
            return []
        if int(active["dimensions"]) != len(query):
            raise ValueError("Query vector dimension does not match the active media index")
        clauses = ["generation_id = ?"]
        arguments: list[object] = [str(active["generation_id"])]
        if logical_document_ids:
            placeholders = ",".join("?" for _item in logical_document_ids)
            clauses.append(f"logical_document_id IN ({placeholders})")
            arguments.extend(logical_document_ids)
        with self._lock:
            rows = self._db.execute(
                "SELECT media_id, logical_document_id, page_no, vector "
                f"FROM media_dense_vectors WHERE {' AND '.join(clauses)}",  # noqa: S608
                arguments,
            ).fetchall()
        scored = sorted(
            ((_dot_blob(query, row["vector"]), row) for row in rows),
            key=lambda item: (-item[0], item[1]["media_id"]),
        )[:limit]
        return [
            DenseMediaHit(
                media_id=row["media_id"],
                logical_document_id=row["logical_document_id"],
                page_no=row["page_no"],
                score=score,
                rank=rank,
            )
            for rank, (score, row) in enumerate(scored, start=1)
        ]

    def clear(self) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM media_dense_vectors")
                self._db.execute("DELETE FROM media_dense_meta")
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise


class LanceDbMediaDenseIndex:
    """Optional ANN implementation using only LanceDB's public synchronous API."""

    def __init__(self, root: Path) -> None:
        try:
            import lancedb
        except ImportError as exc:  # pragma: no cover - depends on optional Haiku extra
            raise RuntimeError("LanceDB media index requires the optional Haiku extra") from exc
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self._meta_path = root / "active-generation.json"
        self._lock = threading.RLock()
        self._db = lancedb.connect(str(root))

    @staticmethod
    def _table_name(generation_id: str, model_digest: str) -> str:
        digest = hashlib.sha256(f"{generation_id}\0{model_digest}".encode()).hexdigest()[:20]
        return f"media_vectors_{digest}"

    def _read_meta(self) -> dict[str, str | int] | None:
        try:
            payload = json.loads(self._meta_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                return None
            return payload
        except (OSError, TypeError, ValueError):
            return None

    def _write_meta(self, payload: Mapping[str, str | int]) -> None:
        temporary = self._meta_path.with_name(f".{self._meta_path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(self._meta_path)
        finally:
            temporary.unlink(missing_ok=True)

    def rebuild(
        self,
        records: Sequence[DenseMediaRecord],
        *,
        generation_id: str,
        model_digest: str,
    ) -> None:
        if not generation_id or not model_digest:
            raise ValueError("Dense media generations require ids and a pinned model digest")
        dimensions = len(records[0].vector) if records else 0
        if any(len(record.vector) != dimensions for record in records):
            raise ValueError("All dense media vectors in a generation must have one dimension")
        normalized = [(record, _normalize(record.vector)) for record in records]
        if len({record.media_id for record, _vector in normalized}) != len(normalized):
            raise ValueError("Dense media generation contains duplicate media ids")
        table_name = self._table_name(generation_id, model_digest)
        with self._lock:
            previous = self._read_meta()
            if normalized:
                self._db.create_table(
                    table_name,
                    data=[
                        {
                            "media_id": record.media_id,
                            "logical_document_id": record.logical_document_id,
                            "page_no": record.page_no,
                            "vector": vector,
                        }
                        for record, vector in normalized
                    ],
                    mode="overwrite",
                )
            self._write_meta(
                {
                    "schema_version": 1,
                    "generation_id": generation_id,
                    "model_digest": model_digest,
                    "dimensions": dimensions,
                    "table_name": table_name if normalized else "",
                }
            )
            previous_table = str(previous.get("table_name", "")) if previous else ""
            if previous_table and previous_table != table_name:
                self._db.drop_table(previous_table, ignore_missing=True)

    def active_generation(self) -> dict[str, str | int] | None:
        with self._lock:
            return self._read_meta()

    @staticmethod
    def _where_documents(logical_document_ids: Sequence[str]) -> str:
        escaped = [f"'{item.replace(chr(39), chr(39) * 2)}'" for item in logical_document_ids]
        return f"logical_document_id IN ({','.join(escaped)})"

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        logical_document_ids: Sequence[str] = (),
    ) -> list[DenseMediaHit]:
        if limit < 1 or limit > 1000:
            raise ValueError("Dense media search limit must be between 1 and 1000")
        query = _normalize(vector)
        with self._lock:
            active = self._read_meta()
            if active is None or not active.get("table_name"):
                return []
            if int(active["dimensions"]) != len(query):
                raise ValueError("Query vector dimension does not match the active media index")
            table = self._db.open_table(str(active["table_name"]))
            search = table.search(query).distance_type("cosine")
            if logical_document_ids:
                search = search.where(self._where_documents(logical_document_ids))
            rows = search.limit(limit).to_list()
        return [
            DenseMediaHit(
                media_id=str(row["media_id"]),
                logical_document_id=str(row["logical_document_id"]),
                page_no=int(row["page_no"]),
                score=max(-1.0, min(1.0, 1.0 - float(row["_distance"]))),
                rank=rank,
            )
            for rank, row in enumerate(rows, start=1)
        ]

    def clear(self) -> None:
        with self._lock:
            for table_name in list(self._db.table_names(limit=10_000)):
                if str(table_name).startswith("media_vectors_"):
                    self._db.drop_table(str(table_name), ignore_missing=True)
            self._meta_path.unlink(missing_ok=True)

    def close(self) -> None:
        # The synchronous embedded connection has no public close method.
        return None


def open_media_dense_index(root: Path, *, prefer_lancedb: bool = True) -> MediaDenseIndex:
    """Open the optional ANN backend, degrading to a separate local SQLite scan."""

    if prefer_lancedb:
        try:
            return LanceDbMediaDenseIndex(root / "media-vectors.lancedb")
        except RuntimeError:
            pass
    return LocalMediaDenseIndex(root / "media-vectors.sqlite3")


@dataclass(frozen=True, slots=True)
class RankedMediaCandidate:
    media_id: str
    score: float
    retrieval_paths: tuple[str, ...]


def build_page_evidence(
    citations: Sequence[Citation],
    *,
    preview_url: Callable[[int, int], str],
    max_pages: int = 12,
) -> list[PageEvidence]:
    """Map answer citations to the page-only half of the shared UI contract."""

    if max_pages < 1 or max_pages > 100:
        raise ValueError("max_pages must be between 1 and 100")
    pages: list[PageEvidence] = []
    seen: set[tuple[str, int]] = set()
    for citation_index, citation in enumerate(citations):
        document_id = citation.logical_document_id or citation.document_id
        source_key = document_id or citation.document_title or f"citation-{citation_index}"
        for page in citation.pages:
            key = (source_key, page)
            if key in seen:
                continue
            seen.add(key)
            pages.append(
                PageEvidence(
                    page_id=f"page-{citation_index}-{page}",
                    citation_index=citation_index,
                    document_id=document_id,
                    document_title=citation.document_title,
                    page=page,
                    score=citation.relevance_score or citation.rerank_score,
                    primary_anchors=[
                        anchor for anchor in citation.primary_anchors if anchor.page == page
                    ],
                    context_anchors=[
                        anchor for anchor in citation.context_anchors if anchor.page == page
                    ],
                    preview_url=preview_url(citation_index, page),
                )
            )
            if len(pages) >= max_pages:
                return pages
    return pages


def fuse_media_rankings(
    rankings: Mapping[str, Sequence[tuple[str, float]]],
    *,
    weights: Mapping[str, float] | None = None,
    rrf_k: int = 60,
) -> list[RankedMediaCandidate]:
    """Weighted reciprocal-rank fusion over caption, evidence and visual routes."""

    if rrf_k < 1:
        raise ValueError("rrf_k must be positive")
    active_weights = dict(weights or {})
    scores: dict[str, float] = {}
    paths: dict[str, set[str]] = {}
    for path, ranking in rankings.items():
        weight = float(active_weights.get(path, 1.0))
        for rank, (media_id, _raw_score) in enumerate(ranking, start=1):
            scores[media_id] = scores.get(media_id, 0.0) + weight / (rrf_k + rank)
            paths.setdefault(media_id, set()).add(path)
    return [
        RankedMediaCandidate(media_id, score, tuple(sorted(paths[media_id])))
        for media_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def select_visual_evidence(
    *,
    query: str,
    pages: Sequence[PagePreviewEvidence],
    assets: Mapping[str, MediaAsset],
    rankings: Mapping[str, Sequence[tuple[str, float]]],
    limit: int = 4,
    min_fused_score: float = 0.0,
    weights: Mapping[str, float] | None = None,
    media_urls: Mapping[str, tuple[str | None, str | None]] | None = None,
    document_titles: Mapping[str, str] | None = None,
    chunk_ids_by_evidence: Mapping[str, str] | None = None,
) -> VisualEvidenceResponse:
    """Select up to four genuine, diverse crops; never fill a quota with noise."""

    if limit < 0 or limit > 4:
        raise ValueError("Visual evidence supports between zero and four media assets")
    fused = fuse_media_rankings(rankings, weights=weights)
    resolved_urls = media_urls or {}
    resolved_titles = document_titles or {}
    resolved_chunks = chunk_ids_by_evidence or {}
    selected: list[VisualEvidence] = []
    selected_hashes: set[str] = set()
    per_page: dict[tuple[str, int], int] = {}
    for candidate in fused:
        if len(selected) >= limit:
            break
        if candidate.score < min_fused_score:
            continue
        asset = assets.get(candidate.media_id)
        if asset is None or asset.crop_resource is None or asset.thumbnail_resource is None:
            continue
        if {"repeated-decoration", "page-background", "crop-unavailable"} & set(
            asset.quality_flags
        ):
            continue
        duplicate_key = asset.pixel_sha256 or asset.perceptual_hash
        if duplicate_key and duplicate_key in selected_hashes:
            continue
        page_key = (asset.logical_document_id, asset.page_no)
        # MMR-like diversity without requiring the dense vectors at response time.
        if per_page.get(page_key, 0) >= 2:
            continue
        if duplicate_key:
            selected_hashes.add(duplicate_key)
        per_page[page_key] = per_page.get(page_key, 0) + 1
        if asset.captions:
            caption_origin = "native-caption"
            caption = asset.captions[0].text
        elif asset.derived_text:
            caption_origin = asset.derived_text[0].origin
            caption = asset.derived_text[0].text
        else:
            caption_origin = None
            caption = None
        thumbnail_url, preview_url = resolved_urls.get(asset.media_id, (None, None))
        selected.append(
            VisualEvidence(
                media_id=asset.media_id,
                kind=asset.kind,
                document_id=asset.logical_document_id,
                document_title=resolved_titles.get(asset.logical_document_id),
                page=asset.page_no,
                bbox=asset.bbox,
                caption=caption,
                caption_origin=caption_origin,
                score=candidate.score,
                evidence_ids=asset.evidence_ids,
                chunk_ids=list(
                    dict.fromkeys(
                        resolved_chunks[evidence_id]
                        for evidence_id in asset.evidence_ids
                        if evidence_id in resolved_chunks
                    )
                ),
                thumbnail_url=thumbnail_url,
                preview_url=preview_url,
                width=asset.width_px,
                height=asset.height_px,
            )
        )
    incomplete = None
    if len(selected) < limit:
        incomplete = "fewer-relevant-assets" if fused else "no-relevant-assets"
    return VisualEvidenceResponse(
        pages=list(pages),
        media=selected,
        selection=VisualEvidenceSelection(
            max_media=limit,
            cut_reason=incomplete,
        ),
    )
