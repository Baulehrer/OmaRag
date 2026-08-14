from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from importlib import metadata as package_metadata
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ruamel.yaml import YAML, YAMLError

from ..models.book import (
    BookBookmark,
    BookLine,
    BookPage,
    BookStructure,
    BookStructureNode,
    EvidenceAnchor,
    EvidenceRecord,
    GlossaryEntry,
    HeadingCandidate,
    IndexEntry,
    NavigationRegion,
    TocEntry,
)
from ..models.domain import BookMetadata, DocumentQuality
from ..models.errors import ConflictError
from ..services.book_knowledge_service import build_bookrag_lite
from ..services.book_snapshot_service import (
    build_book_knowledge_snapshot,
    stable_evidence_id,
)
from ..services.book_structure_service import (
    detect_navigation_regions,
    normalize_book_text,
    parse_glossary,
    parse_reference_list,
    parse_subject_index,
    parse_table_of_contents,
    reconcile_book_structure,
)
from ..services.media_service import (
    MediaVlmEnrichmentResult,
    MediaVlmLimits,
    build_media_snapshot,
    collect_media_assets,
    enrich_media_assets_vlm,
    materialize_collected_media,
)
from ..services.structure_fallback_service import (
    OllamaStructureFallbackRunner,
    StructureFallbackResult,
    StructureFallbackRunner,
    StructureRouteSelection,
    refine_uncertain_navigation_regions,
)

BOOK_V2_PIPELINE = "book-index-v2"
BOOK_V3_PIPELINE = "book-index-v3"
CACHE_SCHEMA = 3
CACHE_KEY_SCHEMA = 1
PageRange = tuple[int, int]


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if item is not None]


def _integers(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    return [int(item) for item in value if item is not None]


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value or ""))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _package_version(name: str) -> str:
    try:
        return package_metadata.version(name)
    except package_metadata.PackageNotFoundError:
        return "unknown"


def _valid_ollama_model_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    valid = (
        len(candidate) <= 256
        and "://" not in candidate
        and not any(character.isspace() for character in candidate)
    )
    return candidate if valid else None


def _same_ollama_model_name(left: str, right: str) -> bool:
    return left.removesuffix(":latest") == right.removesuffix(":latest")


def _configured_vlm_model(config: Any, *, config_path: Path | None = None) -> str | None:
    """Prefer the explicit local VL role, then a vision-enabled Ollama QA model."""

    if config_path is not None:
        try:
            if config_path.is_file() and config_path.stat().st_size <= 2 * 1024**2:
                raw = YAML(typ="safe").load(config_path.read_text(encoding="utf-8")) or {}
                raw_oracle = _value(raw, "oracle", default={}) or {}
                raw_defaults = _value(raw_oracle, "model_defaults", default={}) or {}
                if explicit := _valid_ollama_model_name(_value(raw_defaults, "vl", default=None)):
                    return explicit
        except (OSError, TypeError, ValueError, YAMLError):
            # AppConfig remains authoritative if the optional OmaRAG extension
            # cannot be recovered from the raw YAML mapping.
            pass
    oracle = _value(config, "oracle", default={}) or {}
    defaults = _value(oracle, "model_defaults", default={}) or {}
    if explicit := _valid_ollama_model_name(_value(defaults, "vl", default=None)):
        return explicit
    qa = _value(config, "qa", default={}) or {}
    qa_model = _value(qa, "model", default={}) or {}
    provider = str(_value(qa_model, "provider", default="") or "").casefold()
    vision = bool(_value(qa_model, "vision", default=False))
    if provider != "ollama" or not vision:
        return None
    return _valid_ollama_model_name(_value(qa_model, "name", default=None))


def _configured_vlm_digest(config_path: Path, model: str | None) -> str | None:
    """Return the release-pinned digest, or an empty fail-closed auto-profile marker."""

    try:
        if not config_path.is_file() or config_path.stat().st_size > 2 * 1024**2:
            return None
        raw = YAML(typ="safe").load(config_path.read_text(encoding="utf-8")) or {}
        profile = (_value(raw, "oracle", default={}) or {}).get("model_profile") or {}
        if not isinstance(profile, dict) or profile.get("expert_mode") is not False:
            return None
        artifacts = profile.get("artifacts") or {}
        artifact = artifacts.get("vl") if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict):
            return ""
        if (
            str(artifact.get("provider") or "") != "ollama"
            or not model
            or not _same_ollama_model_name(str(artifact.get("model") or ""), model)
        ):
            return ""
        digest = str(artifact.get("digest") or "")
        return digest if 0 < len(digest) <= 256 else ""
    except (OSError, TypeError, ValueError, YAMLError):
        return ""


def _configured_structure_model(config: Any, *, config_path: Path | None = None) -> str | None:
    """Resolve only the explicit local structure-routing role.

    There is deliberately no QA-model fallback: until a hardware profile owns
    this role, ``llm_fallback=auto`` remains fail-safe and unused unless a
    caller injects both the role and its local runner.
    """

    if config_path is not None:
        try:
            if config_path.is_file() and config_path.stat().st_size <= 2 * 1024**2:
                raw = YAML(typ="safe").load(config_path.read_text(encoding="utf-8")) or {}
                raw_oracle = _value(raw, "oracle", default={}) or {}
                raw_defaults = _value(raw_oracle, "model_defaults", default={}) or {}
                if explicit := _valid_ollama_model_name(
                    _value(raw_defaults, "structure", default=None)
                ):
                    return explicit
        except (OSError, TypeError, ValueError, YAMLError):
            pass
    oracle = _value(config, "oracle", default={}) or {}
    defaults = _value(oracle, "model_defaults", default={}) or {}
    return _valid_ollama_model_name(_value(defaults, "structure", default=None))


def _configured_structure_digest(config_path: Path, model: str | None) -> str | None:
    """Pin an automatic structure role to the shared chat catalog artifact."""

    try:
        if not config_path.is_file() or config_path.stat().st_size > 2 * 1024**2:
            return None
        raw = YAML(typ="safe").load(config_path.read_text(encoding="utf-8")) or {}
        profile = (_value(raw, "oracle", default={}) or {}).get("model_profile") or {}
        if not isinstance(profile, dict) or profile.get("expert_mode") is not False:
            return None
        artifacts = profile.get("artifacts") or {}
        artifact = artifacts.get("chat") if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict):
            return ""
        if (
            str(artifact.get("provider") or "") != "ollama"
            or not model
            or not _same_ollama_model_name(str(artifact.get("model") or ""), model)
        ):
            return ""
        digest = str(artifact.get("digest") or "")
        return digest if 0 < len(digest) <= 256 else ""
    except (OSError, TypeError, ValueError, YAMLError):
        return ""


@dataclass(frozen=True, slots=True)
class PdfBookPreflight:
    total_pages: int
    scanned_pages: list[bool]
    page_labels: dict[str, int]
    bookmarks: list[BookBookmark]


def pdf_preflight(path: Path) -> PdfBookPreflight:
    """Read global PDF signals without rendering or modifying the source."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        total = len(document)
        scanned: list[bool] = []
        page_labels: dict[str, int] = {}
        for page_index in range(total):
            page_no = page_index + 1
            label = str(document.get_page_label(page_index) or page_no).strip()
            page_labels.setdefault(label, page_no)
            page = document[page_index]
            try:
                text_page = page.get_textpage()
                try:
                    text = text_page.get_text_range()
                finally:
                    text_page.close()
            finally:
                page.close()
            scanned.append(len(text.strip()) < 80)
        bookmarks: list[BookBookmark] = []
        for bookmark_index, bookmark in enumerate(document.get_toc(max_depth=30)):
            destination = bookmark.get_dest()
            page_index = destination.get_index() if destination is not None else None
            title = str(bookmark.get_title() or "").strip()
            if not title or page_index is None or not (0 <= page_index < total):
                continue
            bookmarks.append(
                BookBookmark(
                    title=title,
                    depth=int(getattr(bookmark, "level", 0) or 0),
                    page_no=page_index + 1,
                    source_ref=f"pdf-bookmark:{bookmark_index}",
                )
            )
        return PdfBookPreflight(total, scanned, page_labels, bookmarks)
    finally:
        document.close()


def pdf_profile(path: Path) -> tuple[int, list[bool]]:
    """Backward-compatible lightweight view used by older callers/tests."""
    result = pdf_preflight(path)
    return result.total_pages, result.scanned_pages


def _looks_like_memory_pressure(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in ("out of memory", "cannot allocate memory", "memoryerror", "oom")
    )


def _conversion_signature(config: Any, processing_profile: str) -> dict[str, Any]:
    opts = config.processing.conversion_options
    pictures = str(getattr(config.processing, "pictures", "none"))
    economical = processing_profile in {"eco", "low-memory", "fast"}
    return {
        "pipeline": BOOK_V2_PIPELINE,
        "docling": _package_version("docling"),
        "docling_core": _package_version("docling-core"),
        "profile": processing_profile,
        "do_ocr": bool(getattr(opts, "do_ocr", True)),
        "force_ocr": bool(getattr(opts, "force_ocr", False)),
        "ocr_engine": str(getattr(opts, "ocr_engine", "auto")),
        "ocr_lang": list(getattr(opts, "ocr_lang", []) or []),
        "do_table_structure": bool(getattr(opts, "do_table_structure", True)),
        "table_mode": "fast" if economical else str(getattr(opts, "table_mode", "accurate")),
        "table_cell_matching": (
            False if economical else bool(getattr(opts, "table_cell_matching", True))
        ),
        "images_scale": (
            min(float(getattr(opts, "images_scale", 1.0)), 1.0)
            if economical
            else float(getattr(opts, "images_scale", 1.0))
        ),
        "generate_page_images": bool(getattr(opts, "generate_page_images", False)),
        "pictures": pictures,
        "heading_hierarchy": {
            "enabled": True,
            "use_bookmarks": True,
            "use_numbering": True,
            "use_style": True,
            "max_level": 6,
        },
    }


def build_docling_converter(config: Any, processing_profile: str) -> Any:
    """Build exactly one public Docling converter for a complete book run."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        EasyOcrOptions,
        HeadingHierarchyOptions,
        OcrAutoOptions,
        OcrMacOptions,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TableStructureOptions,
        TesseractCliOcrOptions,
        TesseractOcrOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    source_options = config.processing.conversion_options
    signature = _conversion_signature(config, processing_profile)
    language = signature["ocr_lang"]
    force = signature["force_ocr"]
    ocr_types = {
        "easyocr": EasyOcrOptions,
        "ocrmac": OcrMacOptions,
        "rapidocr": RapidOcrOptions,
        "tesseract": TesseractOcrOptions,
        "tesserocr": TesseractCliOcrOptions,
    }
    ocr_type = ocr_types.get(signature["ocr_engine"], OcrAutoOptions)
    ocr_options = ocr_type(force_full_page_ocr=force, lang=language)
    pipeline_options = PdfPipelineOptions(
        do_ocr=signature["do_ocr"],
        do_table_structure=signature["do_table_structure"],
        images_scale=signature["images_scale"],
        generate_page_images=signature["generate_page_images"],
        generate_picture_images=signature["pictures"] != "none",
        generate_parsed_pages=True,
        table_structure_options=TableStructureOptions(
            do_cell_matching=signature["table_cell_matching"],
            mode=(
                TableFormerMode.FAST
                if signature["table_mode"] == "fast"
                else TableFormerMode.ACCURATE
            ),
        ),
        ocr_options=ocr_options,
        heading_hierarchy_options=HeadingHierarchyOptions(
            enabled=True,
            use_bookmarks=True,
            use_numbering=True,
            use_style=True,
            max_level=6,
        ),
    )
    # Picture descriptions require a provider-specific remote-service policy.
    # Book-v2 deliberately keeps conversion local and retains pictures without
    # asking Docling to call an unbrokered model endpoint.
    if str(getattr(config.processing, "pictures", "none")) == "description":
        pipeline_options.generate_picture_images = True
        pipeline_options.do_picture_description = False
    del source_options
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)},
    )


class RangeCache:
    """Compressed content-addressed cache with backward V1.1 reads."""

    def __init__(
        self,
        root: Path,
        *,
        limit_bytes: int = 5 * 1024**3,
        max_age_days: int = 30,
    ) -> None:
        self.root = root
        self.limit_bytes = limit_bytes
        self.max_age_seconds = max_age_days * 24 * 60 * 60

    def key(
        self,
        source_fingerprint: str,
        page_range: PageRange,
        signature: dict[str, Any],
    ) -> str:
        material = json.dumps(
            {
                # Keep the original key namespace so V1.1 JSON entries remain
                # discoverable until natural eviction.
                "schema": CACHE_KEY_SCHEMA,
                "source": source_fingerprint,
                "page_range": list(page_range),
                "conversion": signature,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json.gz"

    def legacy_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def load(self, key: str, page_range: PageRange) -> Any | None:
        path = self.path(key)
        legacy_path = self.legacy_path(key)
        if not path.exists() and not legacy_path.exists():
            return None
        try:
            from docling_core.types.doc import DoclingDocument

            active_path = path if path.exists() else legacy_path
            if active_path.suffix == ".gz":
                with gzip.open(active_path, mode="rt", encoding="utf-8") as source:
                    payload = json.load(source)
            else:
                payload = json.loads(active_path.read_text(encoding="utf-8"))
            if payload.get("schema") not in {1, CACHE_SCHEMA}:
                raise ValueError("unsupported cache schema")
            if tuple(payload.get("page_range", ())) != page_range:
                raise ValueError("page range mismatch")
            document = DoclingDocument.model_validate(payload["document"])
            os.utime(active_path, None)
            return document
        except (KeyError, OSError, TypeError, ValueError):
            path.unlink(missing_ok=True)
            legacy_path.unlink(missing_ok=True)
            return None

    def store(self, key: str, page_range: PageRange, document: Any) -> None:
        if not hasattr(document, "export_to_dict"):
            return
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": CACHE_SCHEMA,
            "page_range": list(page_range),
            "document": document.export_to_dict(),
        }
        with tempfile.NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with gzip.open(temporary_path, mode="wt", encoding="utf-8", compresslevel=6) as target:
            json.dump(payload, target, ensure_ascii=False, separators=(",", ":"))
        temporary_path.replace(self.path(key))

    def prune(self) -> None:
        if not self.root.exists():
            return
        paths = sorted(
            (
                item
                for pattern in ("*.json", "*.json.gz")
                for item in self.root.glob(pattern)
                if item.is_file()
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        retained = 0
        newest_allowed = time.time() - self.max_age_seconds
        for path in paths:
            if path.stat().st_mtime < newest_allowed:
                path.unlink(missing_ok=True)
                continue
            retained += path.stat().st_size
            if retained > self.limit_bytes:
                path.unlink(missing_ok=True)


@dataclass(slots=True)
class HeadingPatchContext:
    logical_document_id: str
    page_range: PageRange
    manifest: HeadingManifest | BookStructure


class HeadingPatchHook(Protocol):
    def __call__(self, context: HeadingPatchContext, chunks: list[Any]) -> list[Any] | None: ...


_NAVIGATION_HEADINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("toc", ("inhaltsverzeichnis", "table of contents", "contents")),
    (
        "subject_index",
        (
            "sachwortverzeichnis",
            "stichwortverzeichnis",
            "schlagwortverzeichnis",
            "subject index",
            "index",
            "register",
        ),
    ),
    ("glossary", ("glossar", "glossary", "begriffsverzeichnis")),
    ("bibliography", ("literaturverzeichnis", "bibliography", "references")),
)
_TOC_LINE = re.compile(r"(?:\.{3,}|\s{3,})\s*\d{1,4}\s*$")
_INDEX_LINE = re.compile(r"^[^\d,]{2,80},\s*\d{1,4}(?:\s*[-,]\s*\d{1,4})*\s*$")


def _navigation_role(title: str) -> str:
    normalized = " ".join(title.casefold().split()).strip(" .:-")
    for role, names in _NAVIGATION_HEADINGS:
        if normalized in names:
            return role
    return "body"


def _content_navigation_role(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) < 3:
        return "body"
    toc = sum(bool(_TOC_LINE.search(line)) for line in lines)
    index = sum(bool(_INDEX_LINE.search(line)) for line in lines)
    if toc >= 3 and toc / len(lines) >= 0.35:
        return "toc"
    if index >= 3 and index / len(lines) >= 0.35:
        return "subject_index"
    return "body"


@dataclass(slots=True)
class HeadingManifest:
    logical_document_id: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    active_ids: list[str] = field(default_factory=list)
    _node_by_id: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _seen: set[tuple[int, int, str]] = field(default_factory=set, repr=False)

    def restore(self, nodes: list[dict[str, Any]], active_ids: list[str]) -> None:
        for raw in nodes:
            node = dict(raw)
            node_id = str(node.get("node_id", ""))
            if not node_id or node_id in self._node_by_id:
                continue
            self.nodes.append(node)
            self._node_by_id[node_id] = node
            self._seen.add(
                (
                    int(node.get("page", 0)),
                    int(node.get("level", 1)),
                    str(node.get("title", "")).casefold(),
                )
            )
        self.active_ids = [node_id for node_id in active_ids if node_id in self._node_by_id]

    def _push(self, title: str, level: int, page: int, doc_item_ref: str) -> None:
        title = " ".join(title.split())
        level = max(1, min(100, level))
        key = (page, level, title.casefold())
        self.active_ids = self.active_ids[: level - 1]
        if key in self._seen:
            existing = next(
                node
                for node in self.nodes
                if (node["page"], node["level"], node["title"].casefold()) == key
            )
            self.active_ids.append(existing["node_id"])
            return
        parent_id = self.active_ids[-1] if self.active_ids else None
        node_id = (
            "heading-"
            + hashlib.sha256(
                f"{self.logical_document_id}\0{page}\0{level}\0{title}\0{parent_id or ''}".encode()
            ).hexdigest()[:20]
        )
        node = {
            "node_id": node_id,
            "parent_id": parent_id,
            "title": title,
            "level": level,
            "page": page,
            "doc_item_ref": doc_item_ref,
            "navigation_role": _navigation_role(title),
            "source": "docling-heading-hierarchy",
        }
        self.nodes.append(node)
        self._node_by_id[node_id] = node
        self._seen.add(key)
        self.active_ids.append(node_id)

    def active_titles(self) -> list[str]:
        return [self._node_by_id[node_id]["title"] for node_id in self.active_ids]

    def active_navigation_role(self) -> str:
        for node_id in reversed(self.active_ids):
            role = str(self._node_by_id[node_id].get("navigation_role", "body"))
            if role != "body":
                return role
        return "body"

    @staticmethod
    def _heading_events(document: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
        events: list[dict[str, Any]] = []
        positions: dict[str, int] = {}
        iterate = getattr(document, "iterate_items", None)
        if not callable(iterate):
            return events, positions
        for position, pair in enumerate(iterate()):
            item, _tree_level = pair
            ref = str(_value(item, "self_ref", default="") or "")
            if ref:
                positions[ref] = position
            label = _enum(_value(item, "label")).casefold().replace("-", "_")
            class_name = type(item).__name__.casefold()
            if label != "section_header" and "sectionheader" not in class_name:
                continue
            provenance = list(_value(item, "prov", default=[]) or [])
            page = int(_value(provenance[0], "page_no", default=0) or 0) if provenance else 0
            events.append(
                {
                    "position": position,
                    "page": page,
                    "level": int(_value(item, "level", default=1) or 1),
                    "title": str(_value(item, "text", default="") or ""),
                    "ref": ref,
                }
            )
        return events, positions

    def patch(
        self,
        document: Any,
        chunks: list[Any],
        page_range: PageRange,
        hook: HeadingPatchHook | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        first_new_node = len(self.nodes)
        events, positions = self._heading_events(document)
        event_index = 0

        def apply_events(position: int | None, page: int | None) -> None:
            nonlocal event_index
            while event_index < len(events):
                event = events[event_index]
                if position is not None:
                    applies = int(event["position"]) <= position
                elif page is not None:
                    applies = int(event["page"]) <= page
                else:
                    applies = False
                if not applies:
                    break
                self._push(
                    str(event["title"]),
                    int(event["level"]),
                    int(event["page"]),
                    str(event["ref"]),
                )
                event_index += 1

        for chunk in sorted(chunks, key=lambda item: int(_value(item, "order", default=0) or 0)):
            chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
            refs = _strings(chunk_metadata.get("doc_item_refs"))
            ref_positions = [positions[ref] for ref in refs if ref in positions]
            pages = _integers(chunk_metadata.get("page_numbers"))
            apply_events(
                min(ref_positions) if ref_positions else None,
                min(pages) if pages else None,
            )
            if pages and any(page < page_range[0] or page > page_range[1] for page in pages):
                raise ConflictError(
                    "Docling returned non-absolute PDF page numbers",
                    details={"page_range": list(page_range), "pages": pages},
                )
            provider_headings = _strings(chunk_metadata.get("headings"))
            headings = self.active_titles() or provider_headings
            role = self.active_navigation_role()
            if role == "body":
                role = _content_navigation_role(str(_value(chunk, "content", default="")))
            embedding_text = "\n".join([*headings, str(_value(chunk, "content", default=""))])
            chunk_metadata.update(
                {
                    "headings": headings,
                    "heading_node_ids": list(self.active_ids),
                    "navigation_role": role,
                    "evidence_role": "raw",
                    "absolute_page_range": list(page_range),
                    "embedding_context_hash": hashlib.sha256(embedding_text.encode()).hexdigest(),
                }
            )
            chunk.metadata = chunk_metadata

        while event_index < len(events):
            event = events[event_index]
            self._push(
                str(event["title"]),
                int(event["level"]),
                int(event["page"]),
                str(event["ref"]),
            )
            event_index += 1

        if hook is not None:
            raw_contents = [str(_value(chunk, "content", default="")) for chunk in chunks]
            raw_pages = [
                _integers((_value(chunk, "metadata", default={}) or {}).get("page_numbers"))
                for chunk in chunks
            ]
            patched = hook(HeadingPatchContext(self.logical_document_id, page_range, self), chunks)
            if patched is not None:
                chunks = patched
            if raw_contents != [str(_value(chunk, "content", default="")) for chunk in chunks]:
                raise ConflictError("A heading patch hook must not modify raw evidence content")
            patched_pages = [
                _integers((_value(chunk, "metadata", default={}) or {}).get("page_numbers"))
                for chunk in chunks
            ]
            if raw_pages != patched_pages:
                raise ConflictError("A heading patch hook must not modify evidence page numbers")
        return chunks, [dict(node) for node in self.nodes[first_new_node:]]

    def export(self) -> dict[str, Any]:
        roles: dict[str, int] = {}
        for node in self.nodes:
            role = str(node.get("navigation_role", "body"))
            roles[role] = roles.get(role, 0) + 1
        return {
            "schema_version": 2,
            "logical_document_id": self.logical_document_id,
            "nodes": [dict(node) for node in self.nodes],
            "active_heading_ids": list(self.active_ids),
            "navigation_heading_counts": roles,
            "fallback_used": not bool(self.nodes),
        }


def _content_chunks(chunks: list[Any]) -> list[Any]:
    excluded = {"page_header", "page_footer"}
    retained: list[Any] = []
    for chunk in chunks:
        labels = {
            label.casefold().replace("-", "_").replace(" ", "_")
            for label in _strings((_value(chunk, "metadata", default={}) or {}).get("labels"))
        }
        if labels and labels <= excluded:
            continue
        retained.append(chunk)
    return retained


def _chunk_contract_payload(chunk: Any, evidence: EvidenceRecord) -> dict[str, Any]:
    chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
    pages = sorted(
        set(_integers(chunk_metadata.get("page_numbers")))
        | {anchor.page_no for anchor in evidence.anchors}
    )
    refs = list(
        dict.fromkeys(
            [
                *_strings(chunk_metadata.get("doc_item_refs")),
                *(anchor.source_ref for anchor in evidence.anchors),
            ]
        )
    )
    return {
        "content_hash": evidence.content_hash,
        "evidence_id": evidence.evidence_id,
        "pages": pages,
        "headings": _strings(chunk_metadata.get("citation_headings"))
        or _strings(chunk_metadata.get("headings")),
        "labels": _strings(chunk_metadata.get("labels")),
        "doc_item_refs": refs,
        "navigation_role": str(chunk_metadata.get("navigation_role", "body")),
        "section_node_id": evidence.section_node_id,
        "aliases": _strings(chunk_metadata.get("aliases")),
        "embedding_aliases": _strings(chunk_metadata.get("embedding_aliases")),
        "reference_roles": _strings(chunk_metadata.get("reference_roles")),
        "context_hash": evidence.context_hash,
        "evidence_kind": evidence.evidence_kind,
        "provenance_kind": evidence.provenance_kind,
        "quality_flags": list(evidence.quality_flags),
    }


def _chunk_contract_hash(chunk: Any, evidence: EvidenceRecord) -> str:
    payload = json.dumps(
        _chunk_contract_payload(chunk, evidence),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _chunk_manifest(
    chunk: Any,
    segment_index: int,
    global_order: int,
    *,
    generation_id: str,
    evidence: EvidenceRecord,
    page_labels: dict[str, int],
) -> dict[str, Any]:
    chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
    raw_content = str(_value(chunk, "content", default=""))
    labels_by_page = {page_no: label for label, page_no in page_labels.items()}
    pages = sorted(
        set(_integers(chunk_metadata.get("page_numbers")))
        | {anchor.page_no for anchor in evidence.anchors}
    )
    refs = list(
        dict.fromkeys(
            [
                *_strings(chunk_metadata.get("doc_item_refs")),
                *(anchor.source_ref for anchor in evidence.anchors),
            ]
        )
    )
    metadata_hash = _chunk_contract_hash(chunk, evidence)
    return {
        "chunk_id": str(_value(chunk, "id", default="") or ""),
        "segment_index": segment_index,
        "chunk_order": global_order,
        "global_order": global_order,
        "generation_id": generation_id,
        "evidence_id": evidence.evidence_id,
        "content_hash": hashlib.sha256(raw_content.encode()).hexdigest(),
        "pages": pages,
        "anchor_page": evidence.page_start,
        "page_labels": [labels_by_page.get(page, str(page)) for page in pages],
        "headings": _strings(chunk_metadata.get("citation_headings"))
        or _strings(chunk_metadata.get("headings")),
        "heading_node_ids": _strings(chunk_metadata.get("heading_node_ids")),
        "labels": _strings(chunk_metadata.get("labels")),
        "doc_item_refs": refs,
        "navigation_role": str(chunk_metadata.get("navigation_role", "body")),
        "evidence_role": "raw",
        "evidence_kind": evidence.evidence_kind,
        "provenance_kind": evidence.provenance_kind,
        "section_node_id": evidence.section_node_id,
        "raw_tokens": evidence.raw_tokens,
        "context_hash": evidence.context_hash,
        "embedding_context_hash": str(chunk_metadata.get("embedding_context_hash", "")),
        "metadata_hash": metadata_hash,
        "aliases": _strings(chunk_metadata.get("aliases")),
        "embedding_aliases": _strings(chunk_metadata.get("embedding_aliases")),
        "reference_roles": _strings(chunk_metadata.get("reference_roles")),
        "previous_evidence_id": evidence.previous_evidence_id,
        "next_evidence_id": evidence.next_evidence_id,
        "quality_flags": list(evidence.quality_flags),
    }


def _book_payload(metadata: BookMetadata | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    payload = metadata.model_dump(mode="json")
    return {
        "book_metadata": payload,
        "work_id": metadata.work_id,
        "book_title": metadata.title,
        "edition_label": metadata.edition_label,
        "edition_number": metadata.edition_number,
        "publication_year": metadata.publication_year,
        "isbn": metadata.isbn,
        "language": metadata.language,
        "curriculum": metadata.curriculum,
        "tags": metadata.tags,
        "document_status": metadata.document_status.value,
    }


@asynccontextmanager
async def _unguarded():
    yield


def _preferred_range_sizes(processing_profile: str) -> tuple[int, int]:
    return {
        "eco": (12, 5),
        "low-memory": (10, 4),
        "default": (25, 10),
        "technical": (25, 10),
        "balanced": (25, 10),
        "quality": (16, 6),
        "image-heavy": (8, 3),
        "fast": (40, 15),
    }.get(processing_profile, (25, 10))


def _plan_ranges(
    scanned_pages: list[bool],
    processing_profile: str,
    segment_sizer: Callable[[int, bool], int] | None,
    *,
    start_page: int = 1,
) -> list[PageRange]:
    text_size, scan_size = _preferred_range_sizes(processing_profile)
    ranges: list[PageRange] = []
    page = start_page
    total = len(scanned_pages)
    while page <= total:
        scanned = scanned_pages[page - 1]
        preferred = scan_size if scanned else text_size
        if segment_sizer is not None:
            preferred = max(1, segment_sizer(preferred, scanned))
        end = min(total, page + preferred - 1)
        for candidate in range(page + 1, end + 1):
            if scanned_pages[candidate - 1] != scanned:
                end = candidate - 1
                break
        ranges.append((page, end))
        page = end + 1
    return ranges


@dataclass(frozen=True, slots=True)
class ConvertedRange:
    # page_range is the disjoint ownership/core interval. conversion_range may
    # include a one-page halo on either side so Docling can preserve semantic
    # units that cross an operational memory boundary.
    page_range: PageRange
    conversion_range: PageRange
    cache_key: str
    cache_hit: bool
    retained_document: Any | None = None


def _conversion_range(core_range: PageRange, total_pages: int, *, halo_pages: int = 1) -> PageRange:
    return (
        max(1, core_range[0] - halo_pages),
        min(total_pages, core_range[1] + halo_pages),
    )


def _docling_bbox(provenance: Any) -> tuple[float, float, float, float] | None:
    bbox = _value(provenance, "bbox")
    if bbox is None:
        return None
    return (
        float(_value(bbox, "l", "left", default=0.0)),
        float(_value(bbox, "t", "top", default=0.0)),
        float(_value(bbox, "r", "right", default=0.0)),
        float(_value(bbox, "b", "bottom", default=0.0)),
    )


def collect_docling_book_signals(
    document: Any,
    page_range: PageRange,
    *,
    page_labels: dict[str, int],
    scanned_pages: list[bool],
) -> tuple[list[BookPage], list[HeadingCandidate]]:
    """Reduce a converted range to lightweight full-book reconciliation input."""
    pages: dict[int, list[BookLine]] = {
        page_no: [] for page_no in range(page_range[0], page_range[1] + 1)
    }
    headings: list[HeadingCandidate] = []
    iterate = getattr(document, "iterate_items", None)
    if callable(iterate):
        for item, _tree_level in iterate():
            text = str(_value(item, "text", default="") or "").strip()
            if not text:
                continue
            reference = str(_value(item, "self_ref", default="") or "") or None
            label = _enum(_value(item, "label")).casefold().replace("-", "_")
            class_name = type(item).__name__.casefold()
            is_heading = label == "section_header" or "sectionheader" in class_name
            provenances = list(_value(item, "prov", default=[]) or [])
            for provenance_index, provenance in enumerate(provenances):
                page_no = int(_value(provenance, "page_no", default=0) or 0)
                if page_no not in pages:
                    continue
                bbox = _docling_bbox(provenance)
                x0, y0, x1, y1 = bbox or (0.0, 0.0, 0.0, 0.0)
                split_lines = [line.strip() for line in text.splitlines() if line.strip()]
                for line_index, line in enumerate(split_lines or [text]):
                    line_ref = (
                        f"{reference}:line:{line_index}"
                        if reference and len(split_lines) > 1
                        else reference
                    )
                    pages[page_no].append(
                        BookLine(
                            page_no=page_no,
                            text=line,
                            x0=x0,
                            y0=y0,
                            x1=x1,
                            y1=y1,
                            source_ref=line_ref,
                        )
                    )
                if is_heading and provenance_index == 0:
                    local_level = _value(item, "level", default=None)
                    headings.append(
                        HeadingCandidate(
                            title=text,
                            page_no=page_no,
                            # Range-local levels are only a weak style fallback.
                            # Bookmark/TOC matches always supply the canonical
                            # full-book depth during reconciliation.
                            level=(int(local_level) if local_level is not None else None),
                            x0=x0,
                            y0=y0,
                            source_ref=reference,
                            confidence=0.65 if local_level is not None else 0.6,
                        )
                    )
    label_by_page = {page_no: label for label, page_no in page_labels.items()}
    book_pages = [
        BookPage(
            page_no=page_no,
            page_label=label_by_page.get(page_no, str(page_no)),
            lines=lines,
            scanned=scanned_pages[page_no - 1],
        )
        for page_no, lines in sorted(pages.items())
    ]
    return book_pages, headings


def _parse_navigation(
    pages: list[BookPage],
    page_labels: dict[str, int],
    *,
    total_pages: int,
    detected_regions: list[NavigationRegion] | None = None,
    route_selections: list[StructureRouteSelection] | None = None,
) -> tuple[
    list[NavigationRegion],
    list[TocEntry],
    list[IndexEntry],
    list[GlossaryEntry],
    dict[str, list[TocEntry]],
]:
    regions = (
        list(detected_regions)
        if detected_regions is not None
        else detect_navigation_regions(pages, total_pages=total_pages)
    )
    selections = list(route_selections or [])

    def apply_route_depths(
        entries: list[TocEntry], role: str, *, model_assisted: bool
    ) -> list[TocEntry]:
        if not model_assisted:
            return entries
        updated: list[TocEntry] = []
        for entry in entries:
            matching = next(
                (
                    selection
                    for selection in selections
                    if selection.role == role
                    and selection.page_no == entry.source_page
                    and selection.locator == entry.locator.raw
                    and (
                        selection.source_ref == entry.source_ref
                        if entry.source_ref is not None
                        else selection.substring in entry.title
                        or entry.title in selection.substring
                    )
                ),
                None,
            )
            if matching is None:
                updated.append(entry)
                continue
            # Model-assisted levels remain explicitly below the immutable
            # deterministic-confidence boundary. Text, page and locator stay
            # byte-for-byte owned by the deterministic parser.
            updated.append(
                entry.model_copy(
                    update={
                        "depth": matching.level,
                        "confidence": min(0.81, matching.objective),
                    }
                )
            )
        return updated

    toc_entries: list[TocEntry] = []
    index_entries: list[IndexEntry] = []
    glossary_entries: list[GlossaryEntry] = []
    reference_entries: dict[str, list[TocEntry]] = {
        "figures": [],
        "tables": [],
        "formulas": [],
    }
    for region in regions:
        if not region.accepted:
            continue
        if region.role == "toc":
            toc_entries.extend(
                apply_route_depths(
                    parse_table_of_contents(pages, region, page_labels),
                    region.role,
                    model_assisted="llm_objective_gain" in region.metrics,
                )
            )
        elif region.role == "index":
            index_entries.extend(parse_subject_index(pages, region, page_labels))
        elif region.role in {"glossary", "abbreviations", "symbols"}:
            glossary_entries.extend(parse_glossary(pages, region))
        elif region.role in reference_entries:
            # Figure/table/equation lists look like a TOC but describe
            # caption targets. They must not become chapter outline anchors.
            reference_entries[region.role].extend(
                apply_route_depths(
                    parse_reference_list(pages, region, page_labels),
                    region.role,
                    model_assisted="llm_objective_gain" in region.metrics,
                )
            )
    return regions, toc_entries, index_entries, glossary_entries, reference_entries


def _node_for_page(structure: BookStructure, page_no: int) -> BookStructureNode:
    candidates = [node for node in structure.nodes if node.page_start <= page_no <= node.page_end]
    if not candidates:
        raise ConflictError(
            "Canonical book structure does not cover an evidence page",
            details={"page": page_no},
        )
    return max(candidates, key=lambda node: (node.depth, node.page_start, -node.ordinal))


def _heading_path(structure: BookStructure, node: BookStructureNode) -> list[str]:
    by_id = {item.node_id: item for item in structure.nodes}
    path: list[str] = []
    current: BookStructureNode | None = node
    seen: set[str] = set()
    while current is not None and current.node_id not in seen:
        seen.add(current.node_id)
        path.append(current.title)
        current = by_id.get(current.parent_id) if current.parent_id else None
    return list(reversed(path))


def _role_for_pages(regions: list[NavigationRegion], pages: list[int]) -> str:
    for region in regions:
        if region.accepted and any(region.page_start <= page <= region.page_end for page in pages):
            return region.role
    return "body"


def _page_alias_index(
    total_pages: int,
    index_entries: list[IndexEntry],
    glossary_entries: list[GlossaryEntry],
    reference_entries: dict[str, list[TocEntry]],
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    aliases: dict[int, list[str]] = {page: [] for page in range(1, total_pages + 1)}
    reference_roles: dict[int, list[str]] = {page: [] for page in range(1, total_pages + 1)}
    for entry in index_entries:
        for locator in entry.locators:
            for page in locator.resolved_pages:
                if page in aliases:
                    aliases[page].extend([entry.term, entry.subterm or ""])
    for entry in glossary_entries:
        if entry.source_page in aliases:
            aliases[entry.source_page].append(entry.term)
    for role, entries in reference_entries.items():
        for entry in entries:
            for page in entry.target_pages:
                if page in aliases:
                    aliases[page].append(entry.title)
                    reference_roles[page].append(role)
    return (
        {
            page: list(dict.fromkeys(alias.strip() for alias in values if alias.strip()))
            for page, values in aliases.items()
        },
        {page: list(dict.fromkeys(values)) for page, values in reference_roles.items()},
    )


def _apply_printed_page_offset(
    entries: list[IndexEntry],
    references: dict[str, list[TocEntry]],
    *,
    offset: int,
    total_pages: int,
) -> tuple[list[IndexEntry], dict[str, list[TocEntry]]]:
    """Apply a globally calibrated Arabic printed-page map to locator evidence."""

    def shifted(locator: Any) -> Any:
        if not offset or not str(locator.start_label).isdigit():
            return locator
        pages = [
            page + offset for page in locator.resolved_pages if 1 <= page + offset <= total_pages
        ]
        return locator.model_copy(update={"resolved_pages": pages})

    mapped_entries = [
        entry.model_copy(update={"locators": [shifted(item) for item in entry.locators]})
        for entry in entries
    ]
    mapped_references = {
        role: [
            entry.model_copy(
                update={
                    "locator": shifted(entry.locator),
                    "target_pages": shifted(entry.locator).resolved_pages,
                }
            )
            for entry in values
        ]
        for role, values in references.items()
    }
    return mapped_entries, mapped_references


def _bounded_embedding_aliases(
    aliases: list[str], *, max_aliases: int = 24, max_tokens: int = 96
) -> list[str]:
    selected: list[str] = []
    token_count = 0
    for alias in aliases:
        alias_tokens = len(re.findall(r"\w+|[^\w\s]", alias, re.UNICODE))
        if selected and (len(selected) >= max_aliases or token_count + alias_tokens > max_tokens):
            break
        selected.append(alias)
        token_count += alias_tokens
    return selected


def patch_chunks_from_structure(
    *,
    logical_document_id: str,
    document: Any,
    chunks: list[Any],
    structure: BookStructure,
    regions: list[NavigationRegion],
    page_aliases: dict[int, list[str]],
    page_reference_roles: dict[int, list[str]],
    page_range: PageRange,
    hook: HeadingPatchHook | None,
) -> list[Any]:
    """Attach canonical full-book context without changing evidence text."""
    for chunk in chunks:
        chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
        pages = _integers(chunk_metadata.get("page_numbers"))
        if not pages:
            pages = [page_range[0]]
            chunk_metadata["page_numbers"] = pages
            quality_flags = ["synthetic-page-anchor"]
        else:
            quality_flags = []
        if any(page < page_range[0] or page > page_range[1] for page in pages):
            raise ConflictError(
                "Docling returned non-absolute PDF page numbers",
                details={"page_range": list(page_range), "pages": pages},
            )
        node = _node_for_page(structure, min(pages))
        headings = _heading_path(structure, node)
        aliases = list(
            dict.fromkeys(alias for page in pages for alias in page_aliases.get(page, []))
        )
        reference_roles = list(
            dict.fromkeys(role for page in pages for role in page_reference_roles.get(page, []))
        )
        chunk_metadata.update(
            {
                "headings": headings,
                "section_node_id": node.node_id,
                "navigation_role": _role_for_pages(regions, pages),
                "aliases": aliases,
                "reference_roles": reference_roles,
                "evidence_role": "raw",
                "absolute_page_range": list(page_range),
                "quality_flags": quality_flags,
            }
        )
        chunk.metadata = chunk_metadata
    if hook is not None:
        raw_contents = [str(_value(chunk, "content", default="")) for chunk in chunks]
        raw_pages = [
            _integers((_value(chunk, "metadata", default={}) or {}).get("page_numbers"))
            for chunk in chunks
        ]
        patched = hook(HeadingPatchContext(logical_document_id, page_range, structure), chunks)
        if patched is not None:
            chunks = patched
        if raw_contents != [str(_value(chunk, "content", default="")) for chunk in chunks]:
            raise ConflictError("A heading patch hook must not modify raw evidence content")
        if raw_pages != [
            _integers((_value(chunk, "metadata", default={}) or {}).get("page_numbers"))
            for chunk in chunks
        ]:
            raise ConflictError("A heading patch hook must not modify evidence page numbers")
    for chunk in chunks:
        chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
        canonical_headings = list(dict.fromkeys(_strings(chunk_metadata.get("headings"))))
        aliases = list(dict.fromkeys(_strings(chunk_metadata.get("aliases"))))
        embedding_aliases = _bounded_embedding_aliases(aliases)
        embedding_headings = list(dict.fromkeys([*canonical_headings, *embedding_aliases]))
        embedding_text = "\n".join([*embedding_headings, str(_value(chunk, "content", default=""))])
        chunk_metadata.update(
            {
                "headings": canonical_headings,
                "citation_headings": canonical_headings,
                "aliases": aliases,
                "embedding_aliases": embedding_aliases,
                "embedding_headings": embedding_headings,
                "embedding_context_hash": hashlib.sha256(
                    embedding_text.encode("utf-8")
                ).hexdigest(),
            }
        )
        chunk.metadata = chunk_metadata
    return chunks


def _item_lookup(document: Any) -> dict[str, Any]:
    iterate = getattr(document, "iterate_items", None)
    if not callable(iterate):
        return {}
    return {
        str(reference): item
        for item, _level in iterate()
        if (reference := _value(item, "self_ref", default=None))
    }


def _evidence_anchors(
    items: dict[str, Any],
    chunk_metadata: dict[str, Any],
    fallback_page: int,
    synthetic_ref: str,
) -> tuple[list[EvidenceAnchor], list[str]]:
    anchors: list[EvidenceAnchor] = []
    quality_flags = list(_strings(chunk_metadata.get("quality_flags")))
    seen: set[tuple[int, str, tuple[float, float, float, float] | None]] = set()
    for reference in _strings(chunk_metadata.get("doc_item_refs")):
        item = items.get(reference)
        if item is None:
            continue
        label = _enum(_value(item, "label")) or None
        for provenance in list(_value(item, "prov", default=[]) or []):
            page_no = int(_value(provenance, "page_no", default=0) or 0)
            if page_no < 1:
                continue
            bbox = _docling_bbox(provenance)
            key = (page_no, reference, bbox)
            if key in seen:
                continue
            seen.add(key)
            anchors.append(
                EvidenceAnchor(
                    page_no=page_no,
                    source_ref=reference,
                    bbox=bbox,
                    label=label,
                )
            )
    if not anchors:
        anchors.append(
            EvidenceAnchor(
                page_no=fallback_page,
                source_ref=synthetic_ref,
                label="page-fallback",
            )
        )
        quality_flags.append("missing-element-provenance")
    anchors.sort(
        key=lambda anchor: (
            anchor.page_no,
            anchor.bbox[1] if anchor.bbox is not None else 0.0,
            anchor.source_ref,
        )
    )
    return anchors, list(dict.fromkeys(quality_flags))


def _typed_evidence(
    chunk_metadata: dict[str, Any],
    anchors: list[EvidenceAnchor],
    quality_flags: list[str],
) -> tuple[str, str]:
    """Classify raw evidence conservatively from provider-owned labels only."""
    labels = {
        value.casefold().replace("-", "_").replace(" ", "_")
        for value in [
            *_strings(chunk_metadata.get("labels")),
            *(anchor.label or "" for anchor in anchors),
        ]
        if value
    }
    navigation_role = str(chunk_metadata.get("navigation_role") or "body")
    if navigation_role != "body":
        evidence_kind = "navigation"
    elif labels & {"table", "table_item"}:
        evidence_kind = "table"
    elif labels & {"formula", "equation"}:
        evidence_kind = "formula"
    elif labels & {"picture", "figure", "image"}:
        evidence_kind = "figure"
    elif any("ocr" in flag.casefold() for flag in quality_flags):
        evidence_kind = "ocr"
    else:
        evidence_kind = "prose" if anchors else "unknown"
    provenance_kind = (
        "page-fallback"
        if "missing-element-provenance" in quality_flags
        or all(anchor.label == "page-fallback" for anchor in anchors)
        else "element"
    )
    return evidence_kind, provenance_kind


def build_evidence_record(
    *,
    document: Any,
    item_lookup: dict[str, Any] | None = None,
    chunk: Any,
    structure: BookStructure,
    fingerprint: str,
    config_hash: str,
    previous_evidence_id: str | None,
) -> EvidenceRecord:
    chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
    pages = _integers(chunk_metadata.get("page_numbers"))
    fallback_page = min(pages) if pages else 1
    chunk_order = int(_value(chunk, "order", default=0) or 0)
    anchors, quality_flags = _evidence_anchors(
        item_lookup if item_lookup is not None else _item_lookup(document),
        chunk_metadata,
        fallback_page,
        f"pdf-page:{fallback_page}:chunk:{chunk_order}",
    )
    pages = sorted({anchor.page_no for anchor in anchors} | set(pages))
    chunk_metadata["page_numbers"] = pages
    chunk_metadata["doc_item_refs"] = list(
        dict.fromkeys(
            [
                *_strings(chunk_metadata.get("doc_item_refs")),
                *(anchor.source_ref for anchor in anchors),
            ]
        )
    )
    raw_content = str(_value(chunk, "content", default=""))
    node = _node_for_page(structure, min(pages))
    evidence_id = stable_evidence_id(fingerprint, config_hash, anchors, raw_content)
    context_hash = str(chunk_metadata.get("embedding_context_hash", "")) or None
    evidence_kind, provenance_kind = _typed_evidence(chunk_metadata, anchors, quality_flags)
    record = EvidenceRecord(
        evidence_id=evidence_id,
        raw_content=raw_content,
        content_hash=hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
        anchors=anchors,
        page_start=min(pages),
        page_end=max(pages),
        section_node_id=node.node_id,
        headings=_strings(chunk_metadata.get("headings")),
        labels=_strings(chunk_metadata.get("labels")),
        aliases=_strings(chunk_metadata.get("aliases")),
        raw_tokens=len(re.findall(r"\w+|[^\w\s]", raw_content, re.UNICODE)),
        context_hash=context_hash,
        previous_evidence_id=previous_evidence_id,
        evidence_kind=evidence_kind,
        provenance_kind=provenance_kind,
        quality_flags=quality_flags,
    )
    chunk_metadata.update(
        {
            "evidence_id": record.evidence_id,
            "previous_evidence_id": previous_evidence_id,
            "section_node_id": record.section_node_id,
            "raw_tokens": record.raw_tokens,
            "context_hash": record.context_hash,
            "evidence_kind": record.evidence_kind,
            "provenance_kind": record.provenance_kind,
            "quality_flags": record.quality_flags,
        }
    )
    chunk.metadata = chunk_metadata
    return record


async def ingest_pdf_book_v2(
    *,
    database: Path,
    source: Path,
    config: Any,
    client_factory: Callable[..., Any],
    haiku_version: str | None,
    processing_profile: str = "default",
    segment_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    before_segment: Callable[[int, int, int], Awaitable[bool]] | None = None,
    generation_id: str | None = None,
    document_fingerprint: str | None = None,
    resume_segments: list[dict[str, Any]] | None = None,
    on_segment: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_phase: Callable[[str, int, int, int], Awaitable[None]] | None = None,
    segment_sizer: Callable[[int, bool], int] | None = None,
    metadata: BookMetadata | None = None,
    original_source: str | None = None,
    indexing_options: dict[str, Any] | None = None,
    llm_url: str | None = None,
    heading_patch_hook: HeadingPatchHook | None = None,
    embed_chunks_fn: Callable[[list[Any], Any, Any], Awaitable[list[Any]]] | None = None,
    structure_fallback_runner: StructureFallbackRunner | None = None,
) -> dict[str, Any]:
    """Index a PDF from immutable bytes using public Docling and Haiku APIs.

    Every conversion receives the original path plus a 1-based inclusive,
    absolute ``page_range``. The same Docling ``DocumentConverter`` instance is
    reused for the complete book. No derived PDFs are created or merged.
    """
    requested_enrichment = str((indexing_options or {}).get("enrichment", "captions")).casefold()
    requested_llm_fallback = str((indexing_options or {}).get("llm_fallback", "auto")).casefold()
    requested_visual_dense = str((indexing_options or {}).get("visual_dense", "off")).casefold()
    defer_previous_generation_retirement = bool(
        (indexing_options or {}).get("_defer_previous_generation_retirement", False)
    )
    if requested_llm_fallback not in {"auto", "off"}:
        raise ConflictError(f"Unsupported local structure fallback mode: {requested_llm_fallback}")
    if requested_visual_dense not in {"off", "on"}:
        raise ConflictError(f"Unsupported visual dense mode: {requested_visual_dense}")
    if requested_visual_dense == "on":
        # The separate media-vector store deliberately has no production
        # encoder until the pinned Media-Goldset proves >=10 pp Recall@5 over
        # caption/graph routing. Accepting the flag without vectors would be a
        # dangerously silent quality regression, so V1.2 fails closed.
        raise ConflictError(
            "Visual Dense has not passed the media quality gate for this release; "
            "use visual_dense=off",
            details={
                "required_gate": "media-gold-recall-at-5-plus-10pp",
                "fallback": "caption-page-graph",
            },
        )
    requested_pipeline = str((indexing_options or {}).get("pipeline", "book-v2")).casefold()
    if requested_pipeline not in {"book-v2", "book-v3", "compatible"}:
        raise ConflictError(f"Unsupported book indexing pipeline: {requested_pipeline}")
    pipeline_version = BOOK_V3_PIPELINE if requested_pipeline == "book-v3" else BOOK_V2_PIPELINE
    boundary_halo_pages = 1 if pipeline_version == BOOK_V3_PIPELINE else 0
    source = source.resolve()
    actual_fingerprint = await asyncio.to_thread(_file_sha256, source)
    if document_fingerprint and document_fingerprint != actual_fingerprint:
        raise ConflictError(
            "PDF fingerprint changed before book-v2 indexing",
            details={
                "path": str(source),
                "expected": document_fingerprint,
                "actual": actual_fingerprint,
            },
        )
    fingerprint = actual_fingerprint
    preflight = await asyncio.to_thread(pdf_preflight, source)
    total_pages = preflight.total_pages
    scanned_pages = preflight.scanned_pages
    if total_pages < 1:
        raise ConflictError(f"PDF {source.name} contains no pages")
    if len(scanned_pages) != total_pages:
        raise ConflictError("PDF preflight returned an inconsistent page count")
    if on_phase is not None:
        await on_phase("profiling", 0, 0, total_pages)

    logical_id = f"book-{fingerprint[:20]}"
    generation_id = generation_id or f"gen-{uuid4().hex[:16]}"
    # Public provenance must not reveal the user's absolute filesystem layout.
    # The managed original remains resolvable through the workspace book record.
    source_uri = f"omarag://documents/{logical_id}/generations/{generation_id}"
    conversion_signature = _conversion_signature(config, processing_profile)
    conversion_signature["pipeline"] = pipeline_version
    conversion_signature["boundary_halo_pages"] = boundary_halo_pages
    pipeline_signature = {
        **conversion_signature,
        "haiku": haiku_version or "unknown",
    }

    def jsonable(value: Any) -> Any:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        if isinstance(value, dict):
            return {str(key): jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted(
                (jsonable(item) for item in value),
                key=lambda item: json.dumps(item, sort_keys=True, default=str),
            )
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict):
            return {
                str(key): jsonable(item)
                for key, item in attributes.items()
                if not str(key).startswith("_")
            }
        return str(value)

    def stable_hash(value: Any) -> str:
        encoded = json.dumps(
            jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    config_hash = stable_hash(
        {
            "pipeline": pipeline_version,
            "conversion": pipeline_signature,
            "processing": jsonable(getattr(config, "processing", None)),
            "embeddings": jsonable(getattr(config, "embeddings", None)),
            "chunker": {
                "type": jsonable(getattr(config.processing, "chunker_type", None)),
                "size": jsonable(getattr(config.processing, "chunk_size", None)),
                "tokenizer": jsonable(getattr(config.processing, "chunking_tokenizer", None)),
                "merge_peers": jsonable(getattr(config.processing, "chunking_merge_peers", None)),
                "markdown_tables": jsonable(
                    getattr(config.processing, "chunking_use_markdown_tables", None)
                ),
            },
            "heading_patch_hook": (
                None
                if heading_patch_hook is None
                else {
                    "module": getattr(heading_patch_hook, "__module__", ""),
                    "qualname": getattr(heading_patch_hook, "__qualname__", ""),
                    "version": getattr(heading_patch_hook, "version", None),
                }
            ),
            "indexing": indexing_options or {},
        }
    )
    cache = RangeCache(database.parent / ".oracle-cache" / "docling-v2")
    # One converter owns all ranges in both passes, including OOM retries.
    converter = build_docling_converter(config, processing_profile)
    guard = segment_guard or _unguarded
    converted: list[ConvertedRange] = []
    book_pages: list[BookPage] = []
    heading_candidates: list[HeadingCandidate] = []
    cache_hits = 0
    converted_ranges = 0
    pass_two_reconversions = 0
    halo_fallback_ranges = 0

    # Pass 1: convert every absolute range and retain only lightweight global
    # signals. Chunking cannot be correct until the complete outline is known.
    pending = [
        (page_range, boundary_halo_pages)
        for page_range in _plan_ranges(scanned_pages, processing_profile, segment_sizer)
    ]
    while pending:
        page_range, halo_pages = pending.pop(0)
        conversion_range = _conversion_range(page_range, total_pages, halo_pages=halo_pages)
        start_page, end_page = page_range
        if before_segment is not None and not await before_segment(
            start_page - 1, end_page, total_pages
        ):
            raise asyncio.CancelledError
        cache_key = cache.key(fingerprint, conversion_range, conversion_signature)
        cache_hit = False
        try:
            async with guard():
                if on_phase is not None:
                    await on_phase("converting", start_page, end_page, total_pages)
                docling_document = await asyncio.to_thread(cache.load, cache_key, conversion_range)
                cache_hit = docling_document is not None
                if docling_document is None:
                    result = await asyncio.to_thread(
                        converter.convert,
                        source,
                        page_range=conversion_range,
                    )
                    docling_document = _value(result, "document", default=result)
                    await asyncio.to_thread(
                        cache.store, cache_key, conversion_range, docling_document
                    )
                    converted_ranges += 1
                else:
                    cache_hits += 1
                pages, headings = collect_docling_book_signals(
                    docling_document,
                    conversion_range,
                    page_labels=preflight.page_labels,
                    scanned_pages=scanned_pages,
                )
                # Halo pages are conversion context, never global-signal owners.
                pages = [page for page in pages if start_page <= page.page_no <= end_page]
                headings = [
                    heading for heading in headings if start_page <= heading.page_no <= end_page
                ]
                # Real Docling documents round-trip through the range cache. A
                # retained fallback keeps custom adapters/tests functional when
                # they expose no Docling-compatible serialization.
                retained = None
                if not cache_hit:
                    try:
                        from docling_core.types.doc import DoclingDocument

                        if not isinstance(docling_document, DoclingDocument):
                            retained = docling_document
                    except ImportError:
                        retained = docling_document
        except Exception as exc:
            if _looks_like_memory_pressure(exc) and start_page < end_page:
                middle = (start_page + end_page) // 2
                pending[0:0] = [
                    ((start_page, middle), boundary_halo_pages),
                    ((middle + 1, end_page), boundary_halo_pages),
                ]
                continue
            if _looks_like_memory_pressure(exc) and halo_pages:
                # At the minimum core size, sacrifice the optional halo rather
                # than failing a book that V1.1 could index safely.
                halo_fallback_ranges += 1
                pending.insert(0, (page_range, 0))
                continue
            raise
        converted.append(
            ConvertedRange(
                page_range,
                conversion_range,
                cache_key,
                cache_hit,
                retained,
            )
        )
        book_pages.extend(pages)
        heading_candidates.extend(headings)

    converted.sort(key=lambda item: item.page_range)
    book_pages.sort(key=lambda item: item.page_no)
    expected_pages = list(range(1, total_pages + 1))
    if [page.page_no for page in book_pages] != expected_pages:
        raise ConflictError(
            "Book-v2 conversion did not cover every absolute PDF page exactly once",
            details={
                "expected_pages": total_pages,
                "observed_pages": [page.page_no for page in book_pages],
            },
        )
    if on_phase is not None:
        await on_phase("reconciling", total_pages, total_pages, total_pages)
    deterministic_regions = detect_navigation_regions(book_pages, total_pages=total_pages)
    structure_fallback = StructureFallbackResult(regions=deterministic_regions)
    if requested_llm_fallback == "auto":
        try:
            structure_config_path = database.parent.parent / "haiku.rag.yaml"
            structure_fallback = await refine_uncertain_navigation_regions(
                pages=book_pages,
                regions=deterministic_regions,
                total_pages=total_pages,
                endpoint=llm_url,
                model=_configured_structure_model(
                    config,
                    config_path=structure_config_path,
                ),
                runner=(structure_fallback_runner or OllamaStructureFallbackRunner()),
                expected_digest=_configured_structure_digest(
                    structure_config_path,
                    _configured_structure_model(config, config_path=structure_config_path),
                ),
                inference_guard=guard,
            )
        except Exception as exc:
            # Model assistance is routing-only and may never turn an otherwise
            # valid deterministic index into a failed import.
            structure_fallback = StructureFallbackResult(
                regions=deterministic_regions,
                candidate_regions=sum(not region.accepted for region in deterministic_regions),
                skipped_regions=sum(not region.accepted for region in deterministic_regions),
                failures=(f"internal-{type(exc).__name__.casefold()}",),
            )
    try:
        (
            regions,
            toc_entries,
            index_entries,
            glossary_entries,
            reference_entries,
        ) = _parse_navigation(
            book_pages,
            preflight.page_labels,
            total_pages=total_pages,
            detected_regions=structure_fallback.regions,
            route_selections=structure_fallback.selections,
        )
    except Exception as exc:
        if not structure_fallback.used:
            raise
        # Even a fully validated proposal remains optional. If a downstream
        # deterministic parser cannot consume it, roll back the whole model
        # contribution and continue with the original regions.
        structure_fallback = StructureFallbackResult(
            regions=deterministic_regions,
            candidate_regions=structure_fallback.candidate_regions,
            calls=structure_fallback.calls,
            applied_regions=0,
            skipped_regions=structure_fallback.candidate_regions,
            failures=(
                *structure_fallback.failures,
                f"application-{type(exc).__name__.casefold()}",
            ),
            model=structure_fallback.model,
        )
        (
            regions,
            toc_entries,
            index_entries,
            glossary_entries,
            reference_entries,
        ) = _parse_navigation(
            book_pages,
            preflight.page_labels,
            total_pages=total_pages,
            detected_regions=deterministic_regions,
        )
    seen_headings: set[tuple[int, str, str | None]] = set()
    unique_headings: list[HeadingCandidate] = []
    for heading in sorted(
        heading_candidates,
        key=lambda item: (item.page_no, item.y0, item.x0, item.title),
    ):
        key = (heading.page_no, normalize_book_text(heading.title), heading.source_ref)
        if key not in seen_headings:
            seen_headings.add(key)
            unique_headings.append(heading)
    structure = reconcile_book_structure(
        logical_id,
        total_pages=total_pages,
        bookmarks=preflight.bookmarks,
        toc_entries=toc_entries,
        headings=unique_headings,
        page_labels=preflight.page_labels,
        regions=regions,
        scanned_pages=(
            page_no for page_no, scanned in enumerate(scanned_pages, start=1) if scanned
        ),
    )
    first_covered_page = min(node.page_start for node in structure.nodes)
    if first_covered_page > 1:
        # Outline anchors often begin at chapter one. Keep the important TOC
        # and front matter as a first-class range instead of assigning it to a
        # later chapter or leaving it structurally uncovered.
        front_title = f"Vorspann Seiten 1–{first_covered_page - 1}"
        front_node = BookStructureNode(
            node_id="sec-"
            + hashlib.sha256(
                f"{logical_id}\0front-matter\0{1}\0{first_covered_page - 1}".encode()
            ).hexdigest()[:24],
            kind="window",
            depth=0,
            ordinal=0,
            title=front_title,
            normalized_title=normalize_book_text(front_title),
            page_start=1,
            page_end=first_covered_page - 1,
            source_kind="window",
            confidence=0.7,
        )
        structure = structure.model_copy(
            update={
                "nodes": [
                    front_node,
                    *[
                        node.model_copy(update={"ordinal": node.ordinal + 1})
                        for node in structure.nodes
                    ],
                ]
            }
        )
    printed_page_offset = int(structure.stats.get("printed_page_offset") or 0)
    index_entries, reference_entries = _apply_printed_page_offset(
        index_entries,
        reference_entries,
        offset=printed_page_offset,
        total_pages=total_pages,
    )
    structure = structure.model_copy(
        update={
            "stats": {
                **structure.stats,
                "index_entry_count": len(index_entries),
                "glossary_entry_count": len(glossary_entries),
                "figure_reference_count": len(reference_entries["figures"]),
                "table_reference_count": len(reference_entries["tables"]),
                "formula_reference_count": len(reference_entries["formulas"]),
                "navigation_region_count": len(regions),
                "accepted_navigation_region_count": sum(region.accepted for region in regions),
                "llm_fallback_candidate_regions": structure_fallback.candidate_regions,
                "llm_fallback_calls": structure_fallback.calls,
                "llm_fallback_applied_regions": structure_fallback.applied_regions,
                "llm_fallback_route_selections": len(structure_fallback.selections),
                "llm_fallback_used": structure_fallback.used,
                "llm_fallback_model": structure_fallback.model,
                "llm_fallback_failures": list(structure_fallback.failures),
            }
        }
    )
    structure_hash = stable_hash(
        {
            "structure": structure.model_dump(mode="json"),
            "toc_entries": [item.model_dump(mode="json") for item in toc_entries],
            "index_entries": [item.model_dump(mode="json") for item in index_entries],
            "glossary_entries": [item.model_dump(mode="json") for item in glossary_entries],
            "reference_entries": {
                role: [item.model_dump(mode="json") for item in entries]
                for role, entries in reference_entries.items()
            },
        }
    )
    page_aliases, page_reference_roles = _page_alias_index(
        total_pages,
        index_entries,
        glossary_entries,
        reference_entries,
    )

    segments: list[dict[str, Any]] = []
    imported_ids: list[str] = []
    manifest_chunks: list[dict[str, Any]] = []
    evidence: list[EvidenceRecord] = []
    media_assets = []
    media_issues: list[str] = []
    vlm_enrichment: MediaVlmEnrichmentResult | None = None
    seen_evidence_ids: set[str] = set()
    exact_duplicate_count = 0
    resumed_ranges = 0
    quality_counts = {
        "chunks": 0,
        "tables": 0,
        "formulas": 0,
        "pictures": 0,
        "refs": 0,
        "navigation": 0,
    }
    snapshot = None

    def candidate_for_range(
        candidates: list[dict[str, Any]], page_range: PageRange
    ) -> dict[str, Any] | None:
        start_page, end_page = page_range
        matches = [
            item
            for item in candidates
            if int(item.get("page_start", 0)) == start_page
            and int(item.get("page_end", 0)) == end_page
            and item.get("generation_id") == generation_id
        ]
        return max(matches, key=lambda item: int(item.get("segment_index", 0)), default=None)

    async with client_factory(database, config=config) as rag:
        listed_documents = await rag.list_documents()
        current_by_id = {
            str(_value(document, "id", default="")): document
            for document in listed_documents
            if _value(document, "id")
        }
        old_document_ids = [
            document_id
            for document_id, document in current_by_id.items()
            if (document_meta := dict(_value(document, "metadata", default={}) or {})).get(
                "logical_document_id"
            )
            == logical_id
            and document_meta.get("generation_id") != generation_id
        ]
        possible_resume_ids = {
            str(candidate.get("document_id", ""))
            for converted_range in converted
            if (
                candidate := candidate_for_range(
                    list(resume_segments or []), converted_range.page_range
                )
            )
            is not None
        }
        for document_id, document in current_by_id.items():
            document_meta = dict(_value(document, "metadata", default={}) or {})
            if (
                document_meta.get("generation_id") == generation_id
                and document_meta.get("logical_document_id") == logical_id
                and document_id not in possible_resume_ids
            ):
                with suppress(Exception):
                    await rag.delete_document(document_id)

        global_order = 0
        for segment_index, converted_range in enumerate(converted):
            page_range = converted_range.page_range
            conversion_range = converted_range.conversion_range
            start_page, end_page = page_range
            # Pass 2 is the expensive provider-facing half. Re-enter the job's
            # pause/cancel gate and resource lease before every range; do not
            # import anything after cancellation has been requested.
            if before_segment is not None and not await before_segment(
                total_pages - 1, total_pages, total_pages
            ):
                raise asyncio.CancelledError
            docling_document = converted_range.retained_document
            if docling_document is None:
                async with guard():
                    docling_document = await asyncio.to_thread(
                        cache.load, converted_range.cache_key, conversion_range
                    )
            if docling_document is None:
                # A concurrent cache prune must not change semantics: reconvert
                # the immutable original with the same book-owned converter.
                async with guard():
                    result = await asyncio.to_thread(
                        converter.convert,
                        source,
                        page_range=conversion_range,
                    )
                    docling_document = _value(result, "document", default=result)
                    await asyncio.to_thread(
                        cache.store,
                        converted_range.cache_key,
                        conversion_range,
                        docling_document,
                    )
                    pass_two_reconversions += 1
            async with guard():
                if on_phase is not None:
                    # Keep the externally visible page cursor monotonic after
                    # pass 1 reached N/N; phase names still expose pass-2 work.
                    await on_phase("chunking", total_pages, total_pages, total_pages)
                chunks = _content_chunks(await rag.chunk(docling_document))
                chunks = patch_chunks_from_structure(
                    logical_document_id=logical_id,
                    document=docling_document,
                    chunks=chunks,
                    structure=structure,
                    regions=regions,
                    page_aliases=page_aliases,
                    page_reference_roles=page_reference_roles,
                    page_range=conversion_range,
                    hook=heading_patch_hook,
                )

            range_pairs: list[tuple[Any, EvidenceRecord]] = []
            range_item_lookup = _item_lookup(docling_document)
            for chunk in chunks:
                previous_id = evidence[-1].evidence_id if evidence else None
                record = build_evidence_record(
                    document=docling_document,
                    item_lookup=range_item_lookup,
                    chunk=chunk,
                    structure=structure,
                    fingerprint=fingerprint,
                    config_hash=config_hash,
                    previous_evidence_id=previous_id,
                )
                owner_page = record.anchors[0].page_no
                if owner_page < start_page or owner_page > end_page:
                    # The halo supplied context to Docling, but another disjoint
                    # core owns this raw evidence. It must never be embedded or
                    # imported twice.
                    continue
                if record.evidence_id in seen_evidence_ids:
                    exact_duplicate_count += 1
                    continue
                if evidence:
                    evidence[-1].next_evidence_id = record.evidence_id
                evidence.append(record)
                seen_evidence_ids.add(record.evidence_id)
                range_pairs.append((chunk, record))
            for chunk, record in range_pairs:
                chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
                chunk_metadata["chunk_contract_hash"] = _chunk_contract_hash(chunk, record)
                chunk.metadata = chunk_metadata

            media_assets.extend(
                asset
                for asset in collect_media_assets(
                    document=docling_document,
                    source_pdf=source,
                    source_fingerprint=fingerprint,
                    logical_document_id=logical_id,
                    generation_id=generation_id,
                    structure=structure,
                    evidence=[record for _chunk, record in range_pairs],
                    page_labels=preflight.page_labels,
                )
                if start_page <= asset.page_no <= end_page
            )

            candidate = candidate_for_range(list(resume_segments or []), page_range)
            candidate_metadata = dict(candidate.get("metadata", {})) if candidate else {}
            candidate_manifest = list(candidate_metadata.get("chunk_manifest", []))
            candidate_document_id = str(candidate.get("document_id", "")) if candidate else ""
            listed_candidate_metadata = (
                dict(
                    _value(
                        current_by_id.get(candidate_document_id),
                        "metadata",
                        default={},
                    )
                    or {}
                )
                if candidate_document_id
                else {}
            )
            resume_valid = (
                bool(candidate)
                and (
                    heading_patch_hook is None or bool(getattr(heading_patch_hook, "version", None))
                )
                and candidate_document_id in current_by_id
                and candidate_metadata.get("pipeline_version") == pipeline_version
                and candidate_metadata.get("config_hash") == config_hash
                and candidate_metadata.get("structure_hash") == structure_hash
                and listed_candidate_metadata.get("logical_document_id") == logical_id
                and listed_candidate_metadata.get("generation_id") == generation_id
                and listed_candidate_metadata.get("pipeline_version") == pipeline_version
                and listed_candidate_metadata.get("config_hash") == config_hash
                and listed_candidate_metadata.get("structure_hash") == structure_hash
                and listed_candidate_metadata.get("fingerprint") == fingerprint
                and int(listed_candidate_metadata.get("page_start", 0)) == start_page
                and int(listed_candidate_metadata.get("page_end", 0)) == end_page
                and len(candidate_manifest) == len(range_pairs)
                and (bool(candidate_manifest) or bool(candidate_metadata.get("empty_range")))
                and all(
                    str(saved.get("content_hash", "")) == record.content_hash
                    and str(saved.get("evidence_id", "")) == record.evidence_id
                    and str(saved.get("context_hash", "")) == str(record.context_hash or "")
                    and str(saved.get("metadata_hash", "")) == _chunk_contract_hash(chunk, record)
                    and bool(saved.get("chunk_id"))
                    for saved, (chunk, record) in zip(candidate_manifest, range_pairs, strict=True)
                )
            )
            if resume_valid:
                for saved in candidate_manifest:
                    stored_chunk = await rag.get_chunk_by_id(str(saved["chunk_id"]))
                    stored_metadata = dict(_value(stored_chunk, "metadata", default={}) or {})
                    if (
                        stored_chunk is None
                        or str(_value(stored_chunk, "document_id", default=""))
                        != candidate_document_id
                        or hashlib.sha256(
                            str(_value(stored_chunk, "content", default="")).encode("utf-8")
                        ).hexdigest()
                        != str(saved["content_hash"])
                        or stored_metadata.get("chunk_contract_hash") != saved.get("metadata_hash")
                    ):
                        resume_valid = False
                        break
            if resume_valid:
                for saved, (chunk, _record) in zip(candidate_manifest, range_pairs, strict=True):
                    chunk.id = str(saved["chunk_id"])
                embedded_chunks = [chunk for chunk, _record in range_pairs]
                document_id = candidate_document_id
                resumed_ranges += 1
            else:
                if (
                    candidate_document_id in current_by_id
                    and listed_candidate_metadata.get("logical_document_id") == logical_id
                    and listed_candidate_metadata.get("generation_id") == generation_id
                ):
                    with suppress(Exception):
                        await rag.delete_document(candidate_document_id)
                if before_segment is not None and not await before_segment(
                    total_pages - 1, total_pages, total_pages
                ):
                    raise asyncio.CancelledError
                if on_phase is not None:
                    await on_phase("embedding", total_pages, total_pages, total_pages)
                if embed_chunks_fn is None:
                    from haiku.rag.embeddings import embed_chunks

                    active_embed_chunks = embed_chunks
                else:
                    active_embed_chunks = embed_chunks_fn
                chunks_to_embed = [chunk for chunk, _record in range_pairs]
                citation_headings: list[list[str]] = []
                # Haiku's public embed primitive contextualizes with headings.
                # A bounded alias set joins the dense and FTS context; raw
                # Chunk.content and the canonical citation path stay separate.
                for chunk in chunks_to_embed:
                    chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
                    headings = _strings(chunk_metadata.get("headings"))
                    citation_headings.append(headings)
                    chunk_metadata["headings"] = _strings(chunk_metadata.get("embedding_headings"))
                    chunk.metadata = chunk_metadata
                async with guard():
                    embedded_chunks = await active_embed_chunks(
                        chunks_to_embed, rag.embedder, config
                    )
                range_pages = list(range(start_page, end_page + 1))
                roles = {_role_for_pages(regions, [page_no]) for page_no in range_pages}
                range_role = next(iter(roles)) if len(roles) == 1 else "mixed"
                document_metadata = {
                    "logical_document_id": logical_id,
                    "generation_id": generation_id,
                    "segment_index": segment_index,
                    "core_start": start_page,
                    "core_end": end_page,
                    "conversion_start": conversion_range[0],
                    "conversion_end": conversion_range[1],
                    "page_start": start_page,
                    "page_end": end_page,
                    "page_offset": 0,
                    "page_number_mode": "absolute",
                    "page_numbers_absolute": True,
                    "role": range_role,
                    "source_uri": source_uri,
                    "parser_id": "docling",
                    "processing_profile": processing_profile,
                    "chunker": "hybrid",
                    "fingerprint": fingerprint,
                    "page_kind": (
                        "scanned"
                        if all(scanned_pages[page - 1] for page in range_pages)
                        else "mixed"
                        if any(scanned_pages[page - 1] for page in range_pages)
                        else "text"
                    ),
                    "cache_key": converted_range.cache_key,
                    "pipeline_version": pipeline_version,
                    "config_hash": config_hash,
                    "structure_hash": structure_hash,
                    **_book_payload(metadata),
                }
                range_uri = (
                    f"{source_uri}#omarag-pages={start_page}-{end_page}&generation={generation_id}"
                )
                if before_segment is not None and not await before_segment(
                    total_pages - 1, total_pages, total_pages
                ):
                    raise asyncio.CancelledError
                async with guard():
                    document = await rag.import_document(
                        docling_document,
                        embedded_chunks,
                        uri=range_uri,
                        title=(document_metadata.get("book_title") or source.name),
                        metadata=document_metadata,
                    )
                # Haiku has already stored the bounded contextual headings for
                # FTS. Restore the in-memory canonical path for manifests and
                # citations; aliases remain explicit metadata.
                for chunk, headings in zip(embedded_chunks, citation_headings, strict=True):
                    chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
                    chunk_metadata["headings"] = headings
                    chunk.metadata = chunk_metadata
                document_id = str(_value(document, "id", "document_id", default=""))
                if not document_id:
                    raise ConflictError("Haiku returned an imported document without an id")
                if on_phase is not None:
                    await on_phase("committing", total_pages, total_pages, total_pages)

            segment_manifest = [
                _chunk_manifest(
                    chunk,
                    segment_index,
                    global_order + offset,
                    generation_id=generation_id,
                    evidence=record,
                    page_labels=preflight.page_labels,
                )
                for offset, (chunk, record) in enumerate(
                    zip(
                        embedded_chunks,
                        [record for _chunk, record in range_pairs],
                        strict=True,
                    )
                )
            ]
            global_order += len(segment_manifest)
            for item in segment_manifest:
                labels = {str(label).casefold() for label in item["labels"]}
                quality_counts["chunks"] += 1
                quality_counts["tables"] += bool(labels & {"table", "table_item"})
                quality_counts["formulas"] += bool(labels & {"formula", "equation"})
                quality_counts["pictures"] += bool(labels & {"picture", "figure", "image"})
                quality_counts["refs"] += bool(item["doc_item_refs"]) and (
                    "missing-element-provenance" not in item["quality_flags"]
                )
                quality_counts["navigation"] += item["navigation_role"] != "body"
            manifest_chunks.extend(segment_manifest)
            imported_ids.append(document_id)
            range_pages = list(range(start_page, end_page + 1))
            page_kind = (
                "scanned"
                if all(scanned_pages[page - 1] for page in range_pages)
                else "mixed"
                if any(scanned_pages[page - 1] for page in range_pages)
                else "text"
            )
            range_roles = {_role_for_pages(regions, [page_no]) for page_no in range_pages}
            range_role = next(iter(range_roles)) if len(range_roles) == 1 else "mixed"
            segment = {
                "document_id": document_id,
                "segment_index": segment_index,
                "page_start": start_page,
                "page_end": end_page,
                "core_start": start_page,
                "core_end": end_page,
                "conversion_start": conversion_range[0],
                "conversion_end": conversion_range[1],
                "page_number_mode": "absolute",
                "role": range_role,
                "fingerprint": fingerprint,
                "generation_id": generation_id,
                "status": "committed",
                "metadata": {
                    "cache_hit": converted_range.cache_hit,
                    "cache_key": converted_range.cache_key,
                    "cache_path": str(cache.path(converted_range.cache_key)),
                    "page_kind": page_kind,
                    "role": range_role,
                    "pipeline_version": pipeline_version,
                    "config_hash": config_hash,
                    "structure_hash": structure_hash,
                    "empty_range": not segment_manifest,
                    "recovered": resume_valid,
                    "chunk_manifest": segment_manifest,
                },
            }
            segments.append(segment)
            if on_segment is not None:
                await on_segment(segment)

        evidence_by_id = {record.evidence_id: record for record in evidence}
        for item in manifest_chunks:
            record = evidence_by_id[item["evidence_id"]]
            item["previous_evidence_id"] = record.previous_evidence_id
            item["next_evidence_id"] = record.next_evidence_id
        graph = build_bookrag_lite(
            structure,
            evidence,
            index_entries=index_entries,
            glossary_entries=glossary_entries,
        )
        media_snapshot = None
        if media_assets:
            try:
                materialized_media = await asyncio.to_thread(
                    materialize_collected_media,
                    source_pdf=source,
                    assets=media_assets,
                    workspace_root=database.parent,
                    expected_fingerprint=fingerprint,
                    expected_generation_id=generation_id,
                    materialize_limit=(
                        MediaVlmLimits().max_crops if requested_enrichment == "vlm" else 0
                    ),
                )
            except Exception as exc:
                materialized_media = [
                    asset.model_copy(
                        update={
                            "quality_flags": [
                                *asset.quality_flags,
                                "crop-unavailable",
                            ]
                        }
                    )
                    for asset in media_assets
                ]
                media_issues.append(
                    f"Medienausschnitte konnten nicht materialisiert werden: {type(exc).__name__}."
                )
            if requested_enrichment == "vlm":
                try:
                    config_path = database.parent.parent / "haiku.rag.yaml"
                    vlm_model = _configured_vlm_model(config, config_path=config_path)
                    vlm_enrichment = await enrich_media_assets_vlm(
                        assets=materialized_media,
                        workspace_root=database.parent,
                        llm_url=llm_url,
                        model=vlm_model,
                        expected_digest=_configured_vlm_digest(config_path, vlm_model),
                        inference_guard=guard,
                    )
                except Exception as exc:
                    # Visual routing hints are optional. A failure must never
                    # invalidate native captions or factual book evidence.
                    vlm_enrichment = MediaVlmEnrichmentResult(
                        assets=materialized_media,
                        eligible_count=sum(
                            asset.crop_resource is not None for asset in materialized_media
                        ),
                        failure=f"internal-{type(exc).__name__.casefold()}",
                    )
                materialized_media = vlm_enrichment.assets
            media_snapshot = build_media_snapshot(
                structure=structure,
                evidence=evidence,
                assets=materialized_media,
                terms=graph.terms,
            )
        elif requested_enrichment == "vlm":
            vlm_enrichment = MediaVlmEnrichmentResult(
                assets=[],
                failure="no-media-assets",
            )
        snapshot = build_book_knowledge_snapshot(
            logical_document_id=logical_id,
            generation_id=generation_id,
            fingerprint=fingerprint,
            config_hash=config_hash,
            structure=structure,
            evidence=evidence,
            graph=graph,
            media=media_snapshot,
        )
        if on_phase is not None:
            await on_phase("verifying", total_pages, total_pages, total_pages)
        final_fingerprint = await asyncio.to_thread(_file_sha256, source)
        if final_fingerprint != fingerprint:
            raise ConflictError(
                "PDF changed while book-v2 indexing was in progress",
                details={
                    "path": str(source),
                    "expected": fingerprint,
                    "actual": final_fingerprint,
                },
            )
        # Standalone adapter users keep the historical behaviour. The daemon
        # instead requests deferred retirement: it first publishes the complete
        # generation in its transactional Store catalogue and only then removes
        # the old Haiku documents under the corpus resource lease. That ordering
        # lets query filters observe either complete generation, never an empty
        # or partially replaced book.
        if not defer_previous_generation_retirement:
            for document_id in old_document_ids:
                with suppress(Exception):
                    await rag.delete_document(document_id)

    await asyncio.to_thread(cache.prune)
    if snapshot is None:  # defensive: the public client context must complete
        raise ConflictError("Book-v2 did not produce a knowledge snapshot")
    provenance_coverage = quality_counts["refs"] / max(quality_counts["chunks"], 1)
    substantive_coverage = (quality_counts["chunks"] - quality_counts["navigation"]) / max(
        quality_counts["chunks"], 1
    )
    issues: list[str] = list(media_issues)
    low_confidence_regions = sum(not region.accepted for region in regions)
    if provenance_coverage < 0.9:
        issues.append("Ein Teil der Chunks besitzt keine elementgenaue PDF-Provenienz.")
    if structure.mode == "window-fallback":
        issues.append(
            "Keine belastbare Gliederung erkannt; deterministische Seitenfenster werden genutzt."
        )
    if requested_llm_fallback == "auto" and low_confidence_regions:
        if structure_fallback.used:
            issues.append(
                "Der lokale Struktur-Fallback verbesserte "
                f"{structure_fallback.applied_regions} Region(en); "
                f"{low_confidence_regions} unsichere Region(en) blieben verworfen."
            )
        else:
            failure = structure_fallback.failures[0] if structure_fallback.failures else "unused"
            issues.append(
                "Unsichere Navigationsregionen wurden deterministisch verworfen; "
                f"der lokale Struktur-Fallback blieb sicher ungenutzt ({failure})."
            )
    if requested_enrichment == "vlm" and vlm_enrichment is not None:
        if not vlm_enrichment.used:
            issues.append(
                "VLM-Anreicherung wurde nicht verwendet "
                f"(0/{vlm_enrichment.eligible_count}, "
                f"abgeschnitten: {vlm_enrichment.truncated_count}, "
                f"Fehler: {vlm_enrichment.failure or 'keine Beschreibung'}); "
                "native Captions und FTS bleiben aktiv."
            )
        elif vlm_enrichment.failure or vlm_enrichment.truncated_count:
            issues.append(
                "VLM-Anreicherung war teilweise erfolgreich "
                f"({vlm_enrichment.enriched_count}/{vlm_enrichment.eligible_count}, "
                f"abgeschnitten: {vlm_enrichment.truncated_count}, "
                f"Fehler: {vlm_enrichment.failure or 'keiner'})."
            )
    if (
        str(getattr(config.processing, "pictures", "none")) == "description"
        and requested_enrichment != "vlm"
    ):
        issues.append(
            "Bildbeschreibungen sind opt-in; indexing.enrichment='vlm' aktiviert "
            "das lokal konfigurierte VL-/QA-Modell."
        )
    toc_found = any(region.accepted and region.role == "toc" for region in regions)
    index_found = any(region.accepted and region.role == "index" for region in regions)
    glossary_found = any(
        region.accepted and region.role in {"glossary", "abbreviations", "symbols"}
        for region in regions
    )
    quality = DocumentQuality(
        score=max(
            0.0,
            min(
                1.0,
                0.55 + 0.25 * provenance_coverage + 0.20 * structure.confidence,
            ),
        ),
        pages_total=total_pages,
        native_text_pages=total_pages - sum(scanned_pages),
        ocr_pages=sum(scanned_pages),
        chunks=quality_counts["chunks"],
        tables=quality_counts["tables"],
        formulas=quality_counts["formulas"],
        pictures=quality_counts["pictures"],
        provenance_coverage=provenance_coverage,
        substantive_coverage=substantive_coverage,
        structure_mode=structure.mode,
        structure_confidence=structure.confidence,
        toc_found=toc_found,
        index_found=index_found,
        glossary_found=glossary_found,
        fallback_used=structure.mode == "window-fallback",
        llm_fallback_used=structure_fallback.used,
        exact_duplicate_count=exact_duplicate_count,
        issues=issues,
    )
    navigation_chunk_counts = {
        role: sum(item["navigation_role"] == role for item in manifest_chunks)
        for role in sorted({item["navigation_role"] for item in manifest_chunks})
    }
    return {
        "source": str(source),
        "source_uri": source_uri,
        "document_id": logical_id,
        "logical_document_id": logical_id,
        "generation_id": generation_id,
        "superseded_segment_document_ids": (
            old_document_ids if defer_previous_generation_retirement else []
        ),
        "segment_document_ids": imported_ids,
        "segments": segments,
        "page_count": total_pages,
        "scanned": any(scanned_pages),
        "scanned_pages": sum(scanned_pages),
        "parser_id": "docling",
        "processing_profile": processing_profile,
        "fingerprint": fingerprint,
        "config_hash": config_hash,
        "structure_hash": structure_hash,
        "book_metadata": metadata.model_dump(mode="json") if metadata else None,
        "original_source": original_source or str(source),
        "managed_source": str(source),
        "pipeline_version": pipeline_version,
        "quality": quality.model_dump(mode="json"),
        "chunk_manifest": manifest_chunks,
        "book_structure": structure.model_dump(mode="json"),
        "book_knowledge_snapshot": snapshot.model_dump(mode="json"),
        "cache_status": (
            "hit" if converted_ranges == 0 else "miss" if cache_hits == 0 else "mixed"
        ),
        "pipeline_stats": {
            "cache_hits": cache_hits,
            "converted_ranges": converted_ranges,
            "pass_two_reconversions": pass_two_reconversions,
            "resumed_ranges": resumed_ranges,
            "range_count": len(converted),
            "boundary_halo_pages": boundary_halo_pages,
            "halo_fallback_ranges": halo_fallback_ranges,
            "core_owned_evidence": True,
            "ocr_pages": sum(scanned_pages),
            "text_pages": total_pages - sum(scanned_pages),
            "vl_pages": len(
                {
                    asset.page_no
                    for asset in snapshot.media.assets
                    if asset.derived_text
                    and any(text.origin == "model-derived" for text in asset.derived_text)
                }
            ),
            "toc_entries": len(toc_entries),
            "index_entries": len(index_entries),
            "glossary_entries": len(glossary_entries),
            "figure_references": len(reference_entries["figures"]),
            "table_references": len(reference_entries["tables"]),
            "formula_references": len(reference_entries["formulas"]),
            "navigation_chunk_counts": navigation_chunk_counts,
            "evidence_records": len(evidence),
            "graph_terms": len(snapshot.graph.terms),
            "graph_edges": len(snapshot.graph.edges),
            "media_assets": len(snapshot.media.assets),
            "media_links": len(snapshot.media.links),
            "media_duplicate_groups": len(snapshot.media.duplicate_groups),
            "two_pass_structure": True,
            "low_confidence_navigation_regions": low_confidence_regions,
            "llm_fallback_requested": requested_llm_fallback,
            "llm_fallback_used": structure_fallback.used,
            "llm_fallback_candidate_regions": structure_fallback.candidate_regions,
            "llm_fallback_calls": structure_fallback.calls,
            "llm_fallback_applied_regions": structure_fallback.applied_regions,
            "llm_fallback_route_selections": len(structure_fallback.selections),
            "llm_fallback_skipped_regions": structure_fallback.skipped_regions,
            "llm_fallback_failures": list(structure_fallback.failures),
            "llm_fallback_model": structure_fallback.model,
            "enrichment_requested": requested_enrichment,
            "vlm_enrichment_used": bool(vlm_enrichment and vlm_enrichment.used),
            "vlm_enrichment_count": (
                vlm_enrichment.enriched_count if vlm_enrichment is not None else 0
            ),
            "vlm_enrichment_eligible": (
                vlm_enrichment.eligible_count if vlm_enrichment is not None else 0
            ),
            "vlm_enrichment_attempted": (
                vlm_enrichment.attempted_count if vlm_enrichment is not None else 0
            ),
            "vlm_enrichment_truncated": bool(vlm_enrichment and vlm_enrichment.truncated_count),
            "vlm_enrichment_truncated_count": (
                vlm_enrichment.truncated_count if vlm_enrichment is not None else 0
            ),
            "vlm_enrichment_failure": (
                vlm_enrichment.failure if vlm_enrichment is not None else None
            ),
            "vlm_model": vlm_enrichment.model if vlm_enrichment is not None else None,
            "vlm_model_digest": (
                vlm_enrichment.model_digest if vlm_enrichment is not None else None
            ),
            "pdf_original_unchanged": True,
            "absolute_page_ranges": True,
            "docling_converter_instances": 1,
        },
    }
