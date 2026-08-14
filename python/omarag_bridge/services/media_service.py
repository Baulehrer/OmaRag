from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import stat
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from PIL import Image

from ..models.book import BookStructure, BookStructureNode, EvidenceRecord, KnowledgeTerm
from ..models.media import (
    BookMediaSnapshot,
    MediaAsset,
    MediaDuplicateGroup,
    MediaLink,
    MediaText,
    NormalizedMediaBBox,
    OKFGenerated,
    OKFMediaProposal,
    OKFMediaSource,
    VisualEvidenceResponse,
)
from .book_structure_service import normalize_book_text

CROP_VERSION = "omarag-media-crop-v1"
_MEDIA_LABELS = {
    "picture": "figure",
    "figure": "figure",
    "image": "figure",
    "diagram": "diagram",
    "chart": "diagram",
    "table": "table",
    "table_item": "table",
    "formula": "formula",
    "equation": "formula",
}


@dataclass(frozen=True, slots=True)
class MediaVlmLimits:
    """Hard bounds for optional local visual enrichment.

    The model path is deliberately slower and less trusted than native PDF
    extraction. It therefore has its own small request, response and wall-time
    budgets and never participates in factual EvidenceRecords.
    """

    max_crops: int = 12
    max_crop_bytes: int = 8 * 1024**2
    max_image_pixels: int = 16_000_000
    max_inventory_bytes: int = 2 * 1024**2
    max_response_bytes: int = 64 * 1024
    request_timeout_seconds: float = 25.0
    total_timeout_seconds: float = 120.0
    description_chars: int = 700

    def __post_init__(self) -> None:
        if not 1 <= self.max_crops <= 64:
            raise ValueError("max_crops must be between 1 and 64")
        if not 1024 <= self.max_crop_bytes <= 32 * 1024**2:
            raise ValueError("max_crop_bytes is outside the safe range")
        if not 1_000_000 <= self.max_image_pixels <= 40_000_000:
            raise ValueError("max_image_pixels is outside the safe range")
        if not 1.0 <= self.request_timeout_seconds <= 60.0:
            raise ValueError("request_timeout_seconds is outside the safe range")
        if not self.request_timeout_seconds <= self.total_timeout_seconds <= 600.0:
            raise ValueError("total_timeout_seconds is outside the safe range")
        if not 100 <= self.description_chars <= 2000:
            raise ValueError("description_chars is outside the safe range")


_DEFAULT_VLM_LIMITS = MediaVlmLimits()


@dataclass(frozen=True, slots=True)
class MediaVlmEnrichmentResult:
    """Auditable result of an opt-in enrichment attempt."""

    assets: list[MediaAsset]
    model: str | None = None
    model_digest: str | None = None
    eligible_count: int = 0
    attempted_count: int = 0
    enriched_count: int = 0
    truncated_count: int = 0
    failure: str | None = None

    @property
    def used(self) -> bool:
        return self.enriched_count > 0


class _MediaVlmError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value or "")).casefold().replace("-", "_")


def _clean_text(value: Any, *, limit: int = 4000) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _stable_id(prefix: str, *values: object) -> str:
    material = "\0".join(str(value) for value in values).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:32]}"


def stable_media_id(
    source_fingerprint: str,
    page_no: int,
    doc_item_ref: str,
    bbox: NormalizedMediaBBox,
    *,
    crop_version: str = CROP_VERSION,
) -> str:
    """Derive identity from immutable source provenance, never from a model output."""

    coordinates = ",".join(f"{value:.6f}" for value in (bbox.x0, bbox.y0, bbox.x1, bbox.y1))
    return _stable_id(
        "media",
        source_fingerprint,
        page_no,
        doc_item_ref,
        coordinates,
        crop_version,
    )


def normalize_media_bbox(
    bbox: Any,
    *,
    page_width: float,
    page_height: float,
    coord_origin: str | None = None,
) -> NormalizedMediaBBox:
    """Normalize a Docling/PDF bbox to top-left coordinates.

    Docling commonly emits PDF points with a bottom-left origin, but cached and
    test documents can contain top-left or already-normalized coordinates.
    The explicit ``coord_origin`` wins; otherwise the t/b ordering is used.
    """

    if page_width <= 0 or page_height <= 0:
        raise ValueError("PDF page dimensions must be positive")
    left = float(_value(bbox, "l", "left", "x0", default=0.0))
    top = float(_value(bbox, "t", "top", "y0", default=0.0))
    right = float(_value(bbox, "r", "right", "x1", default=0.0))
    bottom = float(_value(bbox, "b", "bottom", "y1", default=0.0))
    normalized_input = all(0.0 <= value <= 1.0 for value in (left, top, right, bottom))
    width_scale = 1.0 if normalized_input else page_width
    height_scale = 1.0 if normalized_input else page_height
    x0, x1 = sorted((left / width_scale, right / width_scale))
    raw_y0, raw_y1 = sorted((top / height_scale, bottom / height_scale))
    origin = (coord_origin or _enum(_value(bbox, "coord_origin", default=""))).casefold()
    bottom_left = "bottom" in origin or (not origin and top > bottom)
    if bottom_left:
        y0, y1 = 1.0 - raw_y1, 1.0 - raw_y0
    else:
        y0, y1 = raw_y0, raw_y1
    epsilon = 1e-8
    values = [max(0.0, min(1.0, value)) for value in (x0, y0, x1, y1)]
    if values[2] - values[0] <= epsilon or values[3] - values[1] <= epsilon:
        raise ValueError("Docling media bbox is empty or outside its PDF page")
    return NormalizedMediaBBox(x0=values[0], y0=values[1], x1=values[2], y1=values[3])


def _page_dimensions(
    document: Any, source_pdf: Path, pages: set[int]
) -> dict[int, tuple[float, float]]:
    result: dict[int, tuple[float, float]] = {}
    docling_pages = _value(document, "pages", default={}) or {}
    for page_no in pages:
        page = None
        if isinstance(docling_pages, dict):
            page = docling_pages.get(page_no) or docling_pages.get(str(page_no))
        size = _value(page, "size", default=None)
        width = float(_value(size, "width", default=0.0) or 0.0)
        height = float(_value(size, "height", default=0.0) or 0.0)
        if width > 0 and height > 0:
            result[page_no] = (width, height)
    missing = pages - result.keys()
    if not missing:
        return result
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(source_pdf))
    try:
        for page_no in sorted(missing):
            if page_no < 1 or page_no > len(pdf):
                continue
            page = pdf[page_no - 1]
            try:
                result[page_no] = tuple(float(value) for value in page.get_size())
            finally:
                page.close()
    finally:
        pdf.close()
    return result


def _iter_items(document: Any) -> list[Any]:
    iterate = getattr(document, "iterate_items", None)
    return [item for item, _level in iterate()] if callable(iterate) else []


def _reference(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return _value(value, "cref", "self_ref", default=None)


def _related_texts(item: Any, lookup: dict[str, Any]) -> list[tuple[str, str | None]]:
    values: list[Any] = []
    for field_name in ("captions", "caption", "caption_text"):
        value = _value(item, field_name, default=None)
        if value is None:
            continue
        values.extend(value if isinstance(value, (list, tuple)) else [value])
    texts: list[tuple[str, str | None]] = []
    for value in values:
        reference = _reference(value)
        related = lookup.get(reference, value) if reference else value
        text = _clean_text(_value(related, "text", default=related))
        if text:
            texts.append((text, reference))
    return list(dict.fromkeys(texts))


def _section_for_page(structure: BookStructure, page_no: int) -> BookStructureNode | None:
    candidates = [node for node in structure.nodes if node.page_start <= page_no <= node.page_end]
    return max(candidates, key=lambda node: (node.depth, node.page_start), default=None)


def _matching_evidence(
    evidence: Sequence[EvidenceRecord], page_no: int, media_ref: str
) -> list[EvidenceRecord]:
    candidates = [item for item in evidence if item.page_start <= page_no <= item.page_end]
    return sorted(
        candidates,
        key=lambda item: (
            not any(anchor.source_ref == media_ref for anchor in item.anchors),
            "caption" not in {label.casefold() for label in item.labels},
            item.evidence_id,
        ),
    )[:3]


def collect_media_assets(
    *,
    document: Any,
    source_pdf: Path,
    source_fingerprint: str,
    logical_document_id: str,
    generation_id: str,
    structure: BookStructure,
    evidence: Sequence[EvidenceRecord],
    page_labels: dict[str, int],
    crop_version: str = CROP_VERSION,
) -> list[MediaAsset]:
    """Collect source-bound media metadata from a Docling range.

    This function performs no VL inference. Any text it attaches is native
    Docling text, OCR or an explicit pointer to an existing EvidenceRecord.
    """

    items = _iter_items(document)
    candidates = [
        item
        for item in items
        if _enum(_value(item, "label", default="")) in _MEDIA_LABELS
        or any(marker in type(item).__name__.casefold() for marker in _MEDIA_LABELS)
    ]
    pages = {
        int(_value(provenance, "page_no", default=0) or 0)
        for item in candidates
        for provenance in list(_value(item, "prov", default=[]) or [])
    }
    pages.discard(0)
    if not pages:
        return []
    dimensions = _page_dimensions(document, source_pdf, pages)
    lookup = {
        str(reference): item
        for item in items
        if (reference := _value(item, "self_ref", default=None))
    }
    label_by_page = {page_no: label for label, page_no in page_labels.items()}
    assets: dict[str, MediaAsset] = {}
    for item_index, item in enumerate(candidates):
        label = _enum(_value(item, "label", default=""))
        class_name = type(item).__name__.casefold()
        kind = _MEDIA_LABELS.get(label)
        if kind is None:
            kind = next(
                (_MEDIA_LABELS[marker] for marker in _MEDIA_LABELS if marker in class_name),
                "figure",
            )
        reference = str(_value(item, "self_ref", default="") or f"#/media/{item_index}")
        captions = _related_texts(item, lookup)
        item_text = _clean_text(_value(item, "text", default=""))
        for provenance_index, provenance in enumerate(list(_value(item, "prov", default=[]) or [])):
            page_no = int(_value(provenance, "page_no", default=0) or 0)
            raw_bbox = _value(provenance, "bbox", default=None)
            if page_no not in dimensions or raw_bbox is None:
                continue
            try:
                bbox = normalize_media_bbox(
                    raw_bbox,
                    page_width=dimensions[page_no][0],
                    page_height=dimensions[page_no][1],
                    coord_origin=_enum(_value(raw_bbox, "coord_origin", default="")) or None,
                )
            except ValueError:
                continue
            width = bbox.x1 - bbox.x0
            height = bbox.y1 - bbox.y0
            area = width * height
            if area < 0.0002:
                continue
            touches_page_edges = sum(
                (
                    bbox.x0 <= 0.04,
                    bbox.y0 <= 0.04,
                    bbox.x1 >= 0.96,
                    bbox.y1 >= 0.96,
                )
            )
            if (width >= 0.98 and height >= 0.98) or (area >= 0.88 and touches_page_edges >= 3):
                continue
            page_ref = (
                reference if provenance_index == 0 else f"{reference}:prov:{provenance_index}"
            )
            media_id = stable_media_id(
                source_fingerprint,
                page_no,
                page_ref,
                bbox,
                crop_version=crop_version,
            )
            section = _section_for_page(structure, page_no)
            if section is None:
                continue
            nearby_records = _matching_evidence(evidence, page_no, reference)
            evidence_ids = [record.evidence_id for record in nearby_records]
            native_captions = [
                MediaText(
                    text=text,
                    origin="native-caption",
                    source_ref=caption_ref or reference,
                    confidence=1.0,
                )
                for text, caption_ref in captions[:4]
            ]
            ocr_text = []
            if item_text and item_text not in {caption.text for caption in native_captions}:
                ocr_text.append(
                    MediaText(
                        text=item_text,
                        origin="ocr",
                        source_ref=reference,
                        confidence=0.8,
                    )
                )
            nearby_text = [
                MediaText(
                    text=_clean_text(record.raw_content, limit=1500),
                    origin="nearby-text",
                    source_ref=record.anchors[0].source_ref if record.anchors else None,
                    evidence_id=record.evidence_id,
                    confidence=1.0,
                )
                for record in nearby_records
                if _clean_text(record.raw_content, limit=1500)
            ]
            assets[media_id] = MediaAsset(
                media_id=media_id,
                logical_document_id=logical_document_id,
                generation_id=generation_id,
                source_fingerprint=source_fingerprint,
                page_no=page_no,
                page_label=label_by_page.get(page_no, str(page_no)),
                doc_item_ref=page_ref,
                bbox=bbox,
                kind=kind,
                section_node_id=section.node_id,
                crop_version=crop_version,
                captions=native_captions,
                ocr_text=ocr_text,
                nearby_text=nearby_text,
                evidence_ids=evidence_ids,
                metadata={"docling_label": label, "provenance_index": provenance_index},
            )
    return sorted(
        assets.values(), key=lambda item: (item.page_no, item.doc_item_ref, item.media_id)
    )


def _pixel_sha256(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    material = rgb.width.to_bytes(4, "big") + rgb.height.to_bytes(4, "big") + rgb.tobytes()
    return hashlib.sha256(material).hexdigest()


def perceptual_hash(image: Image.Image, *, hash_size: int = 8) -> str:
    """Compute a deterministic DCT pHash without a numpy runtime dependency."""

    sample_size = hash_size * 4
    resized = image.convert("L").resize((sample_size, sample_size), Image.Resampling.LANCZOS)
    flattened = getattr(resized, "get_flattened_data", resized.getdata)
    pixels = list(flattened())
    coefficients: list[float] = []
    factor = math.pi / (2.0 * sample_size)
    for vertical in range(hash_size):
        for horizontal in range(hash_size):
            value = 0.0
            for y in range(sample_size):
                cos_y = math.cos((2 * y + 1) * vertical * factor)
                offset = y * sample_size
                for x in range(sample_size):
                    value += (
                        pixels[offset + x] * cos_y * math.cos((2 * x + 1) * horizontal * factor)
                    )
            coefficients.append(value)
    threshold = statistics.median(coefficients[1:])
    bits = 0
    for coefficient in coefficients:
        bits = (bits << 1) | int(coefficient > threshold)
    return f"{bits:0{hash_size * hash_size // 4}x}"


def phash_distance(left: str, right: str) -> int:
    if len(left) != len(right):
        raise ValueError("Perceptual hashes must use the same width")
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _atomic_image_save(image: Image.Image, path: Path, *, format_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        if format_name == "WEBP":
            image.save(temporary, format=format_name, lossless=True, method=4)
        else:
            image.save(temporary, format=format_name, optimize=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class MediaRenderLimits:
    crop_long_edge: int = 1800
    thumbnail_long_edge: int = 420
    max_render_pixels: int = 40_000_000
    max_source_bytes: int = 4 * 1024**3
    padding_ratio: float = 0.02


_DEFAULT_RENDER_LIMITS = MediaRenderLimits()


@dataclass(frozen=True, slots=True)
class MediaBlobSweepResult:
    """Conservative mark-and-sweep report for content-addressed media files."""

    dry_run: bool
    marked_digests: int
    candidates: tuple[Path, ...]
    removed: tuple[Path, ...]
    protected: tuple[Path, ...]
    reclaimable_bytes: int
    reclaimed_bytes: int


def materialize_media_crops(
    source_pdf: Path,
    assets: Sequence[MediaAsset],
    output_root: Path,
    *,
    expected_fingerprint: str,
    expected_generation_id: str | None = None,
    limits: MediaRenderLimits = _DEFAULT_RENDER_LIMITS,
) -> list[MediaAsset]:
    """Render all page crops once, verify the immutable source, and share exact blobs."""

    if not assets:
        return []
    source = source_pdf.resolve()
    before = source.stat()
    if not source.is_file() or source.suffix.casefold() != ".pdf":
        raise ValueError("Media crops require an existing local PDF")
    if before.st_size > limits.max_source_bytes:
        raise ValueError("PDF exceeds the configured media-render size limit")
    if _sha256_file(source) != expected_fingerprint:
        raise ValueError("PDF fingerprint changed before media rendering")
    import pypdfium2 as pdfium

    by_page: dict[int, list[tuple[int, MediaAsset]]] = defaultdict(list)
    for index, asset in enumerate(assets):
        if asset.source_fingerprint != expected_fingerprint:
            raise ValueError(f"Media asset {asset.media_id} belongs to another PDF")
        if expected_generation_id is not None and asset.generation_id != expected_generation_id:
            raise ValueError(f"Media asset {asset.media_id} belongs to another generation")
        by_page[asset.page_no].append((index, asset))
    rendered: dict[int, MediaAsset] = {}
    document = pdfium.PdfDocument(str(source))
    try:
        for page_no, page_assets in sorted(by_page.items()):
            if page_no < 1 or page_no > len(document):
                for index, asset in page_assets:
                    rendered[index] = asset.model_copy(
                        update={"quality_flags": [*asset.quality_flags, "page-out-of-range"]}
                    )
                continue
            page = document[page_no - 1]
            try:
                page_width, page_height = page.get_size()
                smallest_extent = min(
                    max(
                        (asset.bbox.x1 - asset.bbox.x0) * page_width,
                        (asset.bbox.y1 - asset.bbox.y0) * page_height,
                    )
                    for _index, asset in page_assets
                )
                target_scale = limits.crop_long_edge / max(smallest_extent, 1.0)
                pixel_cap_scale = math.sqrt(
                    limits.max_render_pixels / max(page_width * page_height, 1.0)
                )
                scale = max(0.5, min(4.0, target_scale, pixel_cap_scale))
                bitmap = page.render(scale=scale)
                try:
                    page_image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
            for index, asset in page_assets:
                padding = limits.padding_ratio
                x0 = max(0.0, asset.bbox.x0 - padding)
                y0 = max(0.0, asset.bbox.y0 - padding)
                x1 = min(1.0, asset.bbox.x1 + padding)
                y1 = min(1.0, asset.bbox.y1 + padding)
                left = max(0, min(page_image.width - 1, round(x0 * page_image.width)))
                top = max(0, min(page_image.height - 1, round(y0 * page_image.height)))
                right = max(left + 1, min(page_image.width, round(x1 * page_image.width)))
                bottom = max(top + 1, min(page_image.height, round(y1 * page_image.height)))
                crop = page_image.crop((left, top, right, bottom))
                if max(crop.size) > limits.crop_long_edge:
                    crop.thumbnail(
                        (limits.crop_long_edge, limits.crop_long_edge), Image.Resampling.LANCZOS
                    )
                pixel_sha = _pixel_sha256(crop)
                phash = perceptual_hash(crop)
                blob_path = output_root / "media" / "blobs" / f"{pixel_sha}.webp"
                thumb_path = output_root / "media" / "thumbnails" / f"{pixel_sha}.webp"
                if not blob_path.exists():
                    _atomic_image_save(crop, blob_path, format_name="WEBP")
                thumbnail = crop.copy()
                thumbnail.thumbnail(
                    (limits.thumbnail_long_edge, limits.thumbnail_long_edge),
                    Image.Resampling.LANCZOS,
                )
                if not thumb_path.exists():
                    _atomic_image_save(thumbnail, thumb_path, format_name="WEBP")
                rendered[index] = asset.model_copy(
                    update={
                        "pixel_sha256": pixel_sha,
                        "perceptual_hash": phash,
                        "width_px": crop.width,
                        "height_px": crop.height,
                        "mime_type": "image/webp",
                        "crop_resource": f"media/blobs/{pixel_sha}.webp",
                        "thumbnail_resource": f"media/thumbnails/{pixel_sha}.webp",
                        "quality_flags": [
                            flag for flag in asset.quality_flags if flag != "crop-unavailable"
                        ],
                    }
                )
    finally:
        document.close()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("PDF changed while media crops were rendered")
    if _sha256_file(source) != expected_fingerprint:
        raise ValueError("PDF fingerprint changed while media crops were rendered")
    return [rendered[index] for index in range(len(assets))]


def _canonical_media_digest(value: str | None) -> str | None:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return digest


def _resource_media_digest(resource: str | None, folder: str) -> str | None:
    if not resource:
        return None
    parts = PurePosixPath(resource).parts
    if len(parts) != 3 or parts[:2] != ("media", folder):
        return None
    filename = parts[2]
    if not filename.endswith(".webp"):
        return None
    return _canonical_media_digest(filename.removesuffix(".webp"))


def media_asset_has_materialized_blobs(output_root: Path, asset: MediaAsset) -> bool:
    """Return whether an asset's exact content-addressed crop pair still exists.

    This deliberately accepts already materialized legacy blobs without needing
    the source PDF. Their immutable digest URL remains useful even if the source
    has since become temporarily unavailable.
    """

    digest = _canonical_media_digest(asset.pixel_sha256)
    if (
        digest is None
        or asset.crop_resource != f"media/blobs/{digest}.webp"
        or asset.thumbnail_resource != f"media/thumbnails/{digest}.webp"
        or asset.mime_type != "image/webp"
        or asset.width_px is None
        or asset.height_px is None
    ):
        return False
    try:
        resolve_media_resource(output_root, asset.crop_resource)
        resolve_media_resource(output_root, asset.thumbnail_resource)
    except (FileNotFoundError, OSError, ValueError):
        return False
    return True


def materialize_selected_media_crops(
    source_pdf: Path,
    assets: Sequence[MediaAsset],
    output_root: Path,
    *,
    expected_fingerprint: str,
    expected_generation_id: str,
    max_assets: int = 4,
    limits: MediaRenderLimits = _DEFAULT_RENDER_LIMITS,
) -> list[MediaAsset]:
    """Materialize one explicitly selected, generation-bound crop set.

    The public helper is intentionally capped and validates the complete set
    before it writes anything. Existing canonical blobs are retained and do not
    require the source PDF to be opened again.
    """

    if not 1 <= max_assets <= 64:
        raise ValueError("max_assets must be between 1 and 64")
    selected = list(assets)
    if len(selected) > max_assets:
        raise ValueError(f"Media materialization is limited to {max_assets} assets")
    if len({asset.media_id for asset in selected}) != len(selected):
        raise ValueError("Media materialization contains duplicate ids")
    for asset in selected:
        if asset.source_fingerprint != expected_fingerprint:
            raise ValueError(f"Media asset {asset.media_id} belongs to another PDF")
        if asset.generation_id != expected_generation_id:
            raise ValueError(f"Media asset {asset.media_id} belongs to another generation")
    if not selected:
        return []

    retained = {
        asset.media_id: asset
        for asset in selected
        if media_asset_has_materialized_blobs(output_root, asset)
    }
    missing = [asset for asset in selected if asset.media_id not in retained]
    if missing:
        rendered = materialize_media_crops(
            source_pdf,
            missing,
            output_root,
            expected_fingerprint=expected_fingerprint,
            expected_generation_id=expected_generation_id,
            limits=limits,
        )
        retained.update((asset.media_id, asset) for asset in rendered)
    return [retained[asset.media_id] for asset in selected]


def mark_media_blob_references(
    assets: Iterable[MediaAsset],
    *,
    protected_digests: Iterable[str] = (),
    visual_evidence: Iterable[VisualEvidenceResponse | dict[str, Any]] = (),
) -> frozenset[str]:
    """Mark every plausible blob digest referenced by active manifests/runs.

    Resource-derived digests are marked independently of ``pixel_sha256``. That
    conservative mismatch handling prevents cleanup from deleting a file while
    a partially migrated manifest still names it.
    """

    marked: set[str] = set()
    for value in protected_digests:
        digest = _canonical_media_digest(value)
        if digest is None:
            raise ValueError("Protected media blob ids must be lowercase SHA-256 digests")
        marked.add(digest)
    marked.update(visual_evidence_blob_digests(visual_evidence))
    for asset in assets:
        for digest in (
            _canonical_media_digest(asset.pixel_sha256),
            _resource_media_digest(asset.crop_resource, "blobs"),
            _resource_media_digest(asset.thumbnail_resource, "thumbnails"),
        ):
            if digest is not None:
                marked.add(digest)
    return frozenset(marked)


def _visual_media_blob_digest(url: str | None) -> str | None:
    """Accept only OmaRAG's immutable local blob route, never arbitrary URLs."""

    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    parts = parsed.path.strip("/").split("/")
    if (
        len(parts) != 7
        or parts[0:2] != ["v1", "workspaces"]
        or parts[3:5] != ["media", "blobs"]
        or parts[6] not in {"crop", "thumbnail"}
    ):
        return None
    return _canonical_media_digest(parts[5])


def visual_evidence_blob_digests(
    responses: Iterable[VisualEvidenceResponse | dict[str, Any]],
) -> frozenset[str]:
    """Extract live blob identities from persisted, generation-pinned runs."""

    marked: set[str] = set()
    for value in responses:
        response = (
            value
            if isinstance(value, VisualEvidenceResponse)
            else VisualEvidenceResponse.model_validate(value)
        )
        for media in response.media:
            for url in (media.thumbnail_url, media.preview_url):
                digest = _visual_media_blob_digest(url)
                if digest is not None:
                    marked.add(digest)
    return frozenset(marked)


def sweep_unreferenced_media_blobs(
    output_root: Path,
    referenced_digests: Iterable[str],
    *,
    dry_run: bool = True,
    minimum_age_seconds: float = 3600.0,
    now: float | None = None,
) -> MediaBlobSweepResult:
    """Remove only old, canonical media files absent from a complete mark set.

    The caller owns enumeration of *all* active manifests and cached run
    selections. Unknown filenames, directories, symlinks, recently written
    files and any marked digest are never deletion candidates.
    """

    if minimum_age_seconds < 0:
        raise ValueError("minimum_age_seconds must not be negative")
    marked = mark_media_blob_references((), protected_digests=referenced_digests)
    cutoff = (time.time() if now is None else now) - minimum_age_seconds
    candidates: list[tuple[Path, os.stat_result]] = []
    protected: list[Path] = []
    configured_media_root = output_root / "media"
    try:
        root_metadata = configured_media_root.lstat()
    except FileNotFoundError:
        root_metadata = None
    if root_metadata is None or not stat.S_ISDIR(root_metadata.st_mode):
        return MediaBlobSweepResult(
            dry_run=dry_run,
            marked_digests=len(marked),
            candidates=(),
            removed=(),
            protected=(),
            reclaimable_bytes=0,
            reclaimed_bytes=0,
        )
    media_root = configured_media_root.resolve()
    for folder in ("blobs", "thumbnails"):
        configured_directory = configured_media_root / folder
        try:
            directory_metadata = configured_directory.lstat()
            if not stat.S_ISDIR(directory_metadata.st_mode):
                protected.append(configured_directory)
                continue
            directory = configured_directory.resolve()
            if not directory.is_relative_to(media_root):
                protected.append(configured_directory)
                continue
            entries = sorted(directory.iterdir())
        except FileNotFoundError:
            continue
        for path in entries:
            digest = _resource_media_digest(f"media/{folder}/{path.name}", folder)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if (
                digest is None
                or not stat.S_ISREG(metadata.st_mode)
                or digest in marked
                or metadata.st_mtime > cutoff
            ):
                protected.append(path)
                continue
            candidates.append((path, metadata))

    removed: list[Path] = []
    reclaimed_bytes = 0
    if not dry_run:
        for path, scanned in candidates:
            try:
                current = path.lstat()
            except FileNotFoundError:
                continue
            identity = (current.st_dev, current.st_ino, current.st_mode, current.st_size)
            scanned_identity = (
                scanned.st_dev,
                scanned.st_ino,
                scanned.st_mode,
                scanned.st_size,
            )
            if identity != scanned_identity or not stat.S_ISREG(current.st_mode):
                protected.append(path)
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed.append(path)
            reclaimed_bytes += current.st_size

    return MediaBlobSweepResult(
        dry_run=dry_run,
        marked_digests=len(marked),
        candidates=tuple(path for path, _metadata in candidates),
        removed=tuple(removed),
        protected=tuple(sorted(set(protected))),
        reclaimable_bytes=sum(metadata.st_size for _path, metadata in candidates),
        reclaimed_bytes=reclaimed_bytes,
    )


def _duplicate_groups(assets: Sequence[MediaAsset]) -> list[MediaDuplicateGroup]:
    exact: dict[str, list[MediaAsset]] = defaultdict(list)
    for asset in assets:
        if asset.pixel_sha256:
            exact[asset.pixel_sha256].append(asset)
    groups = [
        MediaDuplicateGroup(
            canonical_media_id=sorted(items, key=lambda item: item.media_id)[0].media_id,
            member_media_ids=sorted(item.media_id for item in items),
            match="exact",
        )
        for items in exact.values()
        if len(items) > 1
    ]
    exact_representatives = [
        sorted(items, key=lambda item: item.media_id)[0] for items in exact.values() if items
    ]
    sorted_representatives = sorted(exact_representatives, key=lambda item: item.media_id)
    assigned: set[str] = set()
    for index, canonical in enumerate(sorted_representatives):
        if canonical.media_id in assigned or not canonical.perceptual_hash:
            continue
        members = [canonical]
        canonical_ratio = (canonical.width_px or 1) / (canonical.height_px or 1)
        for candidate in sorted_representatives[index + 1 :]:
            if candidate.media_id in assigned or not candidate.perceptual_hash:
                continue
            ratio = (candidate.width_px or 1) / (candidate.height_px or 1)
            if abs(ratio - canonical_ratio) / max(ratio, canonical_ratio) > 0.12:
                continue
            if phash_distance(canonical.perceptual_hash, candidate.perceptual_hash) <= 6:
                members.append(candidate)
                assigned.add(candidate.media_id)
        if len(members) > 1:
            assigned.add(canonical.media_id)
            groups.append(
                MediaDuplicateGroup(
                    canonical_media_id=canonical.media_id,
                    member_media_ids=sorted(item.media_id for item in members),
                    match="perceptual",
                )
            )
    return sorted(groups, key=lambda item: (item.match, item.canonical_media_id))


def _normalized(value: str) -> str:
    return normalize_book_text(value)


def build_media_snapshot(
    *,
    structure: BookStructure,
    evidence: Sequence[EvidenceRecord],
    assets: Sequence[MediaAsset],
    terms: Sequence[KnowledgeTerm] = (),
) -> BookMediaSnapshot:
    if len({asset.media_id for asset in assets}) != len(assets):
        raise ValueError("Media collection contains duplicate ids")
    repeated_small_blobs: set[str] = set()
    by_blob: dict[str, list[MediaAsset]] = defaultdict(list)
    for asset in assets:
        if asset.pixel_sha256:
            by_blob[asset.pixel_sha256].append(asset)
    for pixel_sha, items in by_blob.items():
        page_count = len({item.page_no for item in items})
        small = all(
            (item.bbox.x1 - item.bbox.x0) * (item.bbox.y1 - item.bbox.y0) <= 0.08 for item in items
        )
        undescribed = all(not item.captions and not item.ocr_text for item in items)
        if page_count >= 3 and small and undescribed:
            repeated_small_blobs.add(pixel_sha)
    retained_assets = [asset for asset in assets if asset.pixel_sha256 not in repeated_small_blobs]
    evidence_by_id = {record.evidence_id: record for record in evidence}
    node_ids = {node.node_id for node in structure.nodes}
    for asset in retained_assets:
        if asset.logical_document_id != structure.logical_document_id:
            raise ValueError(f"Media asset {asset.media_id} belongs to another book")
        if asset.section_node_id not in node_ids:
            raise ValueError(f"Media asset {asset.media_id} has an unknown section")
        if any(item not in evidence_by_id for item in asset.evidence_ids):
            raise ValueError(f"Media asset {asset.media_id} has unknown evidence")
    links: dict[str, MediaLink] = {}

    def add_link(link: MediaLink) -> None:
        links.setdefault(link.link_id, link)

    for asset in retained_assets:
        add_link(
            MediaLink(
                link_id=_stable_id(
                    "mlink", asset.section_node_id, asset.media_id, "section_contains_media"
                ),
                source_id=asset.section_node_id,
                target_id=asset.media_id,
                relation="section_contains_media",
                origin="deterministic",
                source_refs=[asset.doc_item_ref],
            )
        )
        for evidence_id in asset.evidence_ids:
            record = evidence_by_id[evidence_id]
            direct = any(anchor.source_ref == asset.doc_item_ref for anchor in record.anchors)
            relation = "evidence_depicts_media" if direct else "evidence_context_for_media"
            add_link(
                MediaLink(
                    link_id=_stable_id("mlink", evidence_id, asset.media_id, relation),
                    source_id=evidence_id,
                    target_id=asset.media_id,
                    relation=relation,
                    origin="deterministic",
                    evidence_ids=[evidence_id],
                    source_refs=[asset.doc_item_ref],
                )
            )
        source_text = _normalized(
            " ".join(item.text for item in [*asset.captions, *asset.ocr_text])
        )
        if source_text:
            for term in terms:
                if term.normalized and f" {term.normalized} " in f" {source_text} ":
                    add_link(
                        MediaLink(
                            link_id=_stable_id(
                                "mlink", asset.media_id, term.term_id, "media_mentions_term"
                            ),
                            source_id=asset.media_id,
                            target_id=term.term_id,
                            relation="media_mentions_term",
                            origin="source-text",
                            evidence_ids=asset.evidence_ids,
                            source_refs=[asset.doc_item_ref],
                        )
                    )
        for derived in asset.derived_text:
            derived_text = _normalized(derived.text)
            if not derived_text:
                continue
            for term in terms:
                if not term.normalized or f" {term.normalized} " not in f" {derived_text} ":
                    continue
                origin = "model-derived" if derived.origin == "model-derived" else "human-reviewed"
                add_link(
                    MediaLink(
                        link_id=_stable_id(
                            "mlink",
                            asset.media_id,
                            term.term_id,
                            "media_mentions_term",
                            origin,
                            derived.model_digest or "",
                        ),
                        source_id=asset.media_id,
                        target_id=term.term_id,
                        relation="media_mentions_term",
                        origin=origin,
                        weight=derived.confidence,
                        evidence_ids=[],
                        source_refs=[asset.doc_item_ref],
                        model_digest=derived.model_digest,
                    )
                )
    duplicate_groups = _duplicate_groups(retained_assets)
    for group in duplicate_groups:
        relation = "media_duplicate_of" if group.match == "exact" else "media_variant_of"
        for member in group.member_media_ids:
            if member == group.canonical_media_id:
                continue
            add_link(
                MediaLink(
                    link_id=_stable_id("mlink", member, group.canonical_media_id, relation),
                    source_id=member,
                    target_id=group.canonical_media_id,
                    relation=relation,
                    origin="deterministic",
                )
            )
    return BookMediaSnapshot(
        assets=sorted(retained_assets, key=lambda item: (item.page_no, item.media_id)),
        links=sorted(links.values(), key=lambda item: item.link_id),
        duplicate_groups=duplicate_groups,
    )


def build_okf_media_proposal(
    asset: MediaAsset,
    *,
    source_document_resource: str,
    source_title: str | None = None,
) -> OKFMediaProposal:
    """Map Core media to a deterministic OKF proposal without writing a bundle."""

    if asset.pixel_sha256 is None or asset.crop_resource is None:
        raise ValueError("OKF media proposals require a materialized source crop")
    type_by_kind = {
        "figure": "Book Figure",
        "diagram": "Book Diagram",
        "table": "Book Table",
        "formula": "Book Formula",
    }
    caption = next(iter(asset.captions), None)
    reviewed = next((item for item in asset.derived_text if item.origin == "human-reviewed"), None)
    generated_text = next(
        (item for item in asset.derived_text if item.origin == "model-derived"), None
    )
    description_item = caption or reviewed or generated_text
    title = (
        _clean_text(caption.text, limit=160)
        if caption is not None
        else f"{type_by_kind[asset.kind]} – Seite {asset.page_label}"
    )
    generated = (
        OKFGenerated(actor="OmaRAG visual enrichment", model_digest=generated_text.model_digest)
        if description_item is generated_text
        and generated_text is not None
        and generated_text.model_digest is not None
        else None
    )
    return OKFMediaProposal(
        type=type_by_kind[asset.kind],  # type: ignore[arg-type]
        title=title,
        description=description_item.text if description_item is not None else None,
        resource=f"/references/media/sha256/{asset.pixel_sha256}.webp",
        sources=[
            OKFMediaSource(
                resource=source_document_resource,
                title=source_title,
                page=asset.page_no,
                source_ref=asset.doc_item_ref,
            )
        ],
        generated=generated,
        omarag={
            "media": {
                "media_id": asset.media_id,
                "logical_document_id": asset.logical_document_id,
                "generation_id": asset.generation_id,
                "source_fingerprint": asset.source_fingerprint,
                "page": asset.page_no,
                "page_label": asset.page_label,
                "bbox": asset.bbox.model_dump(mode="json"),
                "doc_item_ref": asset.doc_item_ref,
                "pixel_sha256": asset.pixel_sha256,
                "perceptual_hash": asset.perceptual_hash,
                "crop_version": asset.crop_version,
                "section_node_id": asset.section_node_id,
                "evidence_ids": asset.evidence_ids,
            },
            "derived": {
                "vl": [
                    item.model_dump(mode="json")
                    for item in asset.derived_text
                    if item.origin == "model-derived"
                ]
            },
        },
    )


def materialize_collected_media(
    *,
    source_pdf: Path,
    assets: Iterable[MediaAsset],
    workspace_root: Path,
    expected_fingerprint: str,
    expected_generation_id: str | None = None,
    materialize_limit: int | None = None,
) -> list[MediaAsset]:
    """Compose metadata-only, bounded-VLM or legacy full materialization.

    ``materialize_limit=0`` is the V1.2 metadata-only indexing path. A positive
    limit pre-materializes only a deterministic crop set for an explicitly
    requested VLM. ``None`` preserves the older full-materialization behavior
    until the Book-v2 caller opts into the V1.2 mode.
    """

    unique = {asset.media_id: asset for asset in assets}
    ordered = sorted(unique.values(), key=lambda item: (item.page_no, item.media_id))
    for asset in ordered:
        if asset.source_fingerprint != expected_fingerprint:
            raise ValueError(f"Media asset {asset.media_id} belongs to another PDF")
        if expected_generation_id is not None and asset.generation_id != expected_generation_id:
            raise ValueError(f"Media asset {asset.media_id} belongs to another generation")
    if materialize_limit is not None and not 0 <= materialize_limit <= 64:
        raise ValueError("materialize_limit must be between 0 and 64")
    if not ordered or materialize_limit == 0:
        return ordered

    if materialize_limit is None:
        return materialize_media_crops(
            source_pdf,
            ordered,
            workspace_root,
            expected_fingerprint=expected_fingerprint,
            expected_generation_id=expected_generation_id,
        )

    kind_priority = {"diagram": 0, "table": 1, "formula": 2, "figure": 3}
    selected = sorted(
        ordered,
        key=lambda asset: (
            bool(asset.captions or asset.ocr_text),
            kind_priority[asset.kind],
            -((asset.bbox.x1 - asset.bbox.x0) * (asset.bbox.y1 - asset.bbox.y0)),
            asset.page_no,
            asset.media_id,
        ),
    )[:materialize_limit]
    generation_id = expected_generation_id or selected[0].generation_id
    rendered = materialize_selected_media_crops(
        source_pdf,
        selected,
        workspace_root,
        expected_fingerprint=expected_fingerprint,
        expected_generation_id=generation_id,
        max_assets=materialize_limit,
    )
    updates = {asset.media_id: asset for asset in rendered}
    return [updates.get(asset.media_id, asset) for asset in ordered]


def add_derived_media_text(
    asset: MediaAsset,
    *,
    text: str,
    model_digest: str,
    confidence: float,
) -> MediaAsset:
    """Attach a provenance-pinned VL routing hint without changing evidence links."""

    derived = MediaText(
        text=_clean_text(text),
        origin="model-derived",
        source_ref=asset.doc_item_ref,
        model_digest=model_digest,
        confidence=confidence,
    )
    existing = {(item.text, item.origin, item.model_digest): item for item in asset.derived_text}
    existing[(derived.text, derived.origin, derived.model_digest)] = derived
    return asset.model_copy(
        update={
            "derived_text": sorted(
                existing.values(),
                key=lambda item: (item.origin, item.text, item.model_digest or ""),
            )
        }
    )


_VLM_DESCRIPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "minLength": 3, "maxLength": 700},
        "visible_terms": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
            "maxItems": 10,
        },
    },
    "required": ["description", "visible_terms"],
    "additionalProperties": False,
}


def _same_ollama_model(left: str, right: str) -> bool:
    return left == right or left == f"{right}:latest" or right == f"{left}:latest"


async def _bounded_ollama_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    max_bytes: int,
    timeout: httpx.Timeout,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with client.stream(method, path, json=payload, timeout=timeout) as response:
        response.raise_for_status()
        length = response.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > max_bytes:
                    raise _MediaVlmError("response-too-large")
            except ValueError as exc:
                raise _MediaVlmError("invalid-content-length") from exc
        body = bytearray()
        async for part in response.aiter_bytes():
            body.extend(part)
            if len(body) > max_bytes:
                raise _MediaVlmError("response-too-large")
    try:
        result = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _MediaVlmError("invalid-json") from exc
    if not isinstance(result, dict):
        raise _MediaVlmError("invalid-json-object")
    return result


async def _installed_vlm_identity(
    client: httpx.AsyncClient,
    model: str,
    *,
    limits: MediaVlmLimits,
    timeout: httpx.Timeout,
) -> tuple[str, str]:
    payload = await _bounded_ollama_json(
        client,
        "GET",
        "/api/tags",
        max_bytes=limits.max_inventory_bytes,
        timeout=timeout,
    )
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise _MediaVlmError("invalid-model-inventory")
    matches = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name") or raw.get("model")
        digest = raw.get("digest")
        if (
            isinstance(name, str)
            and isinstance(digest, str)
            and digest
            and _same_ollama_model(name, model)
        ):
            matches.append(raw)
    if not matches:
        raise _MediaVlmError("model-not-installed")
    exact = [raw for raw in matches if (raw.get("name") or raw.get("model")) == model]
    if len(matches) > 1 and len(exact) != 1:
        raise _MediaVlmError("ambiguous-model-name")
    selected = exact[0] if exact else matches[0]
    if selected.get("remote_host") or selected.get("remote_model"):
        raise _MediaVlmError("model-not-local")
    name = str(selected.get("name") or selected.get("model"))
    digest = str(selected["digest"])
    if len(name) > 256 or len(digest) > 256:
        raise _MediaVlmError("invalid-model-identity")
    capabilities = selected.get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        normalized = {str(item).casefold() for item in capabilities}
        if "vision" not in normalized:
            raise _MediaVlmError("model-not-vision-capable")
    return name, digest


def _read_verified_crop(
    workspace_root: Path,
    asset: MediaAsset,
    *,
    limits: MediaVlmLimits,
) -> str:
    if (
        asset.pixel_sha256 is None
        or asset.crop_resource is None
        or asset.thumbnail_resource is None
        or asset.width_px is None
        or asset.height_px is None
    ):
        raise _MediaVlmError("crop-not-materialized")
    expected = (workspace_root / "media" / "blobs" / f"{asset.pixel_sha256}.webp").resolve()
    path = resolve_media_resource(
        workspace_root,
        asset.crop_resource,
        max_bytes=limits.max_crop_bytes,
    )
    if path != expected:
        raise _MediaVlmError("crop-resource-mismatch")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limits.max_crop_bytes:
            raise _MediaVlmError("invalid-crop-file")
        chunks: list[bytes] = []
        total = 0
        while part := os.read(descriptor, min(1024 * 1024, limits.max_crop_bytes + 1 - total)):
            total += len(part)
            if total > limits.max_crop_bytes:
                raise _MediaVlmError("crop-too-large")
            chunks.append(part)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "WEBP":
                raise _MediaVlmError("invalid-crop-format")
            width, height = image.size
            if width * height > limits.max_image_pixels:
                raise _MediaVlmError("crop-pixel-limit")
            image.load()
            if (width, height) != (asset.width_px, asset.height_px):
                raise _MediaVlmError("crop-dimension-mismatch")
            if _pixel_sha256(image) != asset.pixel_sha256:
                raise _MediaVlmError("crop-digest-mismatch")
    except _MediaVlmError:
        raise
    except (OSError, ValueError) as exc:
        raise _MediaVlmError("invalid-crop-image") from exc
    return base64.b64encode(raw).decode("ascii")


def _parse_vlm_description(payload: dict[str, Any], *, limits: MediaVlmLimits) -> str:
    if payload.get("done") is not True:
        raise _MediaVlmError("incomplete-chat-response")
    message = payload.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise _MediaVlmError("invalid-chat-response")
    try:
        content = json.loads(message["content"])
    except json.JSONDecodeError as exc:
        raise _MediaVlmError("invalid-description-json") from exc
    if not isinstance(content, dict) or set(content) != {"description", "visible_terms"}:
        raise _MediaVlmError("invalid-description-object")
    raw_description = content.get("description")
    raw_terms = content.get("visible_terms")
    if (
        not isinstance(raw_description, str)
        or len(raw_description) > limits.description_chars
        or not isinstance(raw_terms, list)
        or len(raw_terms) > 10
        or any(not isinstance(item, str) or len(item) > 80 for item in raw_terms)
    ):
        raise _MediaVlmError("invalid-description-fields")
    description = _clean_text(raw_description, limit=limits.description_chars)
    if not description:
        raise _MediaVlmError("invalid-description-fields")
    terms = list(
        dict.fromkeys(
            term
            for item in raw_terms
            if (term := _clean_text(item, limit=80))
            and term.casefold() not in description.casefold()
        )
    )
    routing = description
    if terms:
        routing = f"{description} Sichtbare Begriffe: {', '.join(terms)}."
    return _clean_text(routing, limit=limits.description_chars)


def _vlm_failure_code(exc: Exception) -> str:
    if isinstance(exc, _MediaVlmError):
        return exc.code
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http-{exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "ollama-unavailable"
    return type(exc).__name__.casefold()


def _eligible_materialized_assets(assets: Sequence[MediaAsset]) -> list[MediaAsset]:
    return [
        asset
        for asset in assets
        if asset.pixel_sha256
        and asset.crop_resource == f"media/blobs/{asset.pixel_sha256}.webp"
        and asset.thumbnail_resource == f"media/thumbnails/{asset.pixel_sha256}.webp"
        and asset.mime_type == "image/webp"
        and asset.width_px is not None
        and asset.height_px is not None
        and "crop-unavailable" not in asset.quality_flags
    ]


def _is_local_ollama_url(value: str) -> bool:
    """Return true only for an explicit loopback HTTP endpoint."""

    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            return False
        if host.casefold() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


async def enrich_media_assets_vlm(
    *,
    assets: Sequence[MediaAsset],
    workspace_root: Path,
    llm_url: str | None,
    model: str | None,
    expected_digest: str | None = None,
    limits: MediaVlmLimits = _DEFAULT_VLM_LIMITS,
    client: httpx.AsyncClient | None = None,
    inference_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> MediaVlmEnrichmentResult:
    """Add bounded, digest-pinned Ollama VL descriptions as routing text only.

    This is an explicitly invoked enrichment step. It only calls ``/api/tags``
    and ``/api/chat``; it never pulls a model. Any inventory, crop, protocol or
    inference failure returns the original assets so native caption/FTS routing
    and the factual book index remain usable.
    """

    originals = list(assets)
    eligible = _eligible_materialized_assets(originals)
    if not eligible:
        return MediaVlmEnrichmentResult(
            assets=originals,
            eligible_count=0,
            failure="no-materialized-crops",
        )
    if not llm_url:
        return MediaVlmEnrichmentResult(
            assets=originals,
            eligible_count=len(eligible),
            failure="ollama-endpoint-not-configured",
        )
    if not _is_local_ollama_url(llm_url):
        return MediaVlmEnrichmentResult(
            assets=originals,
            eligible_count=len(eligible),
            failure="ollama-endpoint-not-local",
        )
    if not model or len(model) > 256 or any(character.isspace() for character in model):
        return MediaVlmEnrichmentResult(
            assets=originals,
            eligible_count=len(eligible),
            failure="vlm-model-not-configured",
        )

    by_pixel: dict[str, list[MediaAsset]] = defaultdict(list)
    for asset in eligible:
        assert asset.pixel_sha256 is not None
        by_pixel[asset.pixel_sha256].append(asset)
    groups = sorted(
        by_pixel.values(),
        key=lambda group: (
            bool(group[0].captions or group[0].ocr_text),
            {"diagram": 0, "table": 1, "formula": 2, "figure": 3}[group[0].kind],
            -((group[0].bbox.x1 - group[0].bbox.x0) * (group[0].bbox.y1 - group[0].bbox.y0)),
            group[0].page_no,
            group[0].media_id,
        ),
    )
    selected = groups[: limits.max_crops]
    truncated_count = sum(len(group) for group in groups[limits.max_crops :])
    request_timeout = httpx.Timeout(
        timeout=limits.request_timeout_seconds,
        connect=min(2.0, limits.request_timeout_seconds),
        pool=min(2.0, limits.request_timeout_seconds),
    )
    owns_client = client is None
    http = client or httpx.AsyncClient(
        base_url=llm_url.rstrip("/"),
        timeout=request_timeout,
        follow_redirects=False,
        trust_env=False,
    )
    attempted = 0
    updates: dict[str, MediaAsset] = {}
    failure_codes: list[str] = []
    resolved_name: str | None = None
    resolved_digest: str | None = None
    try:
        async with asyncio.timeout(limits.total_timeout_seconds):
            resolved_name, resolved_digest = await _installed_vlm_identity(
                http,
                model,
                limits=limits,
                timeout=request_timeout,
            )
            if expected_digest is not None and not hmac.compare_digest(
                expected_digest.removeprefix("sha256:").casefold(),
                resolved_digest.removeprefix("sha256:").casefold(),
            ):
                raise _MediaVlmError("catalog-digest-mismatch")
            for group in selected:
                representative = group[0]
                try:
                    encoded_crop = await asyncio.to_thread(
                        _read_verified_crop,
                        workspace_root,
                        representative,
                        limits=limits,
                    )
                    attempted += 1
                    request_payload = {
                        "model": resolved_name,
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "Beschreibe ausschließlich, was im freigestellten "
                                    f"Buch-{representative.kind} direkt sichtbar ist. "
                                    "Keine Vermutungen, kein Wissen außerhalb des Bildes, "
                                    "keine Seitenbeschreibung. Nenne sichtbare Fachbegriffe "
                                    "für die Suche. Antworte exakt im vorgegebenen JSON-Schema."
                                ),
                                "images": [encoded_crop],
                            },
                        ],
                        "stream": False,
                        "think": False,
                        "keep_alive": "2m",
                        "format": _VLM_DESCRIPTION_SCHEMA,
                        "options": {
                            "temperature": 0,
                            "seed": 0,
                            "num_ctx": 2048,
                            "num_predict": 192,
                        },
                    }
                    if inference_guard is None:
                        response = await _bounded_ollama_json(
                            http,
                            "POST",
                            "/api/chat",
                            max_bytes=limits.max_response_bytes,
                            timeout=request_timeout,
                            payload=request_payload,
                        )
                    else:
                        # One admission per crop lets waiting chats pre-empt a
                        # long optional enrichment batch between images.
                        async with inference_guard():
                            response = await _bounded_ollama_json(
                                http,
                                "POST",
                                "/api/chat",
                                max_bytes=limits.max_response_bytes,
                                timeout=request_timeout,
                                payload=request_payload,
                            )
                    returned_model = response.get("model")
                    if not isinstance(returned_model, str) or not _same_ollama_model(
                        returned_model, resolved_name
                    ):
                        raise _MediaVlmError("chat-model-mismatch")
                    description = _parse_vlm_description(response, limits=limits)
                    for asset in group:
                        updates[asset.media_id] = add_derived_media_text(
                            asset,
                            text=description,
                            model_digest=resolved_digest,
                            confidence=0.45,
                        )
                except Exception as exc:
                    failure_codes.append(_vlm_failure_code(exc))
            if updates:
                verified_name, verified_digest = await _installed_vlm_identity(
                    http,
                    model,
                    limits=limits,
                    timeout=request_timeout,
                )
                if verified_name != resolved_name or verified_digest != resolved_digest:
                    raise _MediaVlmError("model-digest-changed")
    except TimeoutError:
        return MediaVlmEnrichmentResult(
            assets=originals,
            model=resolved_name or model,
            model_digest=resolved_digest,
            eligible_count=len(eligible),
            attempted_count=attempted,
            truncated_count=truncated_count,
            failure="total-timeout-unverified",
        )
    except Exception as exc:
        return MediaVlmEnrichmentResult(
            assets=originals,
            model=resolved_name or model,
            model_digest=resolved_digest,
            eligible_count=len(eligible),
            attempted_count=attempted,
            truncated_count=truncated_count,
            failure=_vlm_failure_code(exc),
        )
    finally:
        if owns_client:
            await http.aclose()

    enriched_assets = [updates.get(asset.media_id, asset) for asset in originals]
    failure = None
    if failure_codes:
        counts = {code: failure_codes.count(code) for code in sorted(set(failure_codes))}
        failure = ",".join(f"{code}:{count}" for code, count in counts.items())
    return MediaVlmEnrichmentResult(
        assets=enriched_assets,
        model=resolved_name,
        model_digest=resolved_digest,
        eligible_count=len(eligible),
        attempted_count=attempted,
        enriched_count=len(updates),
        truncated_count=truncated_count,
        failure=failure,
    )


def resolve_media_resource(
    output_root: Path,
    resource: str,
    *,
    max_bytes: int = 32 * 1024**2,
) -> Path:
    """Resolve a stored crop/thumbnail below the workspace media directory."""

    root = (output_root / "media").resolve()
    candidate = (output_root / resource).resolve()
    if not candidate.is_relative_to(root) or candidate.suffix.casefold() != ".webp":
        raise ValueError("Media resource is outside the workspace media directory")
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    if candidate.stat().st_size > max_bytes:
        raise ValueError("Media resource exceeds the response size limit")
    return candidate
