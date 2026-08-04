from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import io
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from importlib import metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..models.domain import (
    BookMetadata,
    CapabilitySet,
    Citation,
    CitationAnchor,
    DocumentQuality,
    EvidenceMode,
    SearchHit,
)
from ..models.errors import AdapterUnavailableError, ConflictError
from .base import HaikuAdapter


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if item is not None]


def _int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    return [int(item) for item in value if item is not None]


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _page_size(document: Any, page_no: int) -> tuple[float, float] | None:
    pages = _value(document, "pages", default={}) or {}
    page = pages.get(page_no) or pages.get(str(page_no))
    size = _value(page, "size")
    width = _value(size, "width")
    height = _value(size, "height")
    if not width or not height:
        return None
    return float(width), float(height)


def _anchors_for_refs(
    document: Any,
    refs: list[str],
    *,
    page_offset: int = 0,
) -> list[CitationAnchor]:
    """Resolve Docling refs without depending on private Haiku internals."""
    try:
        from docling_core.types.doc.document import RefItem
    except ImportError:
        return []

    anchors: list[CitationAnchor] = []
    seen: set[tuple[Any, ...]] = set()
    for ref in refs:
        try:
            item = RefItem.model_validate({"$ref": ref}).resolve(document)
        except Exception:
            continue
        label = _enum_value(_value(item, "label"))
        for provenance in _value(item, "prov", default=[]) or []:
            local_page = int(_value(provenance, "page_no", default=0) or 0)
            bbox = _value(provenance, "bbox")
            size = _page_size(document, local_page)
            if local_page < 1 or bbox is None or size is None:
                continue
            width, height = size
            to_top_left = getattr(bbox, "to_top_left_origin", None)
            if callable(to_top_left):
                bbox = to_top_left(height)
            left = float(_value(bbox, "l", "left", default=0.0))
            right = float(_value(bbox, "r", "right", default=0.0))
            top = float(_value(bbox, "t", "top", default=0.0))
            bottom = float(_value(bbox, "b", "bottom", default=0.0))
            # Docling inputs may use either coordinate origin. Convert through
            # its public helper before normalizing for image/terminal clients.
            x0 = max(0.0, min(1.0, min(left, right) / width))
            x1 = max(0.0, min(1.0, max(left, right) / width))
            y0 = max(0.0, min(1.0, min(top, bottom) / height))
            y1 = max(0.0, min(1.0, max(top, bottom) / height))
            key = (local_page, ref, round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6))
            if key in seen or x1 <= x0 or y1 <= y0:
                continue
            seen.add(key)
            anchors.append(
                CitationAnchor(
                    page=local_page + page_offset,
                    doc_item_ref=ref,
                    element_type=label,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                )
            )
    return anchors


def _looks_like_memory_pressure(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("out of memory", "cannot allocate memory", "oom", "memoryerror")
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _pdf_info(path: Path) -> tuple[int, list[bool]]:
    """Return page count and a cheap per-page text-vs-scan classification."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        total = len(document)
        scanned_pages: list[bool] = []
        for index in range(total):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    text = text_page.get_text_range()
                finally:
                    text_page.close()
            finally:
                page.close()
            scanned_pages.append(len(text.strip()) < 80)
        return total, scanned_pages
    finally:
        document.close()


def _cached_pdf_info(path: Path, profile_path: Path) -> tuple[int, list[bool]]:
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        scanned_pages = payload["scanned_pages"]
        if payload.get("version") == 1 and isinstance(scanned_pages, list):
            os.utime(profile_path, None)
            return len(scanned_pages), [bool(value) for value in scanned_pages]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result = _pdf_info(path)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = profile_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "scanned_pages": result[1]}, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(profile_path)
    return result


def _pdf_slice(path: Path, start: int, end: int) -> bytes:
    """Extract zero-based ``[start, end)`` pages into a standalone PDF."""
    import pypdfium2 as pdfium

    source = pdfium.PdfDocument(str(path))
    target = pdfium.PdfDocument.new()
    try:
        target.import_pages(source, list(range(start, end)))
        output = io.BytesIO()
        target.save(output)
        return output.getvalue()
    finally:
        target.close()
        source.close()


def _cache_file(database: Path, cache_key: str) -> Path:
    return database.parent / ".oracle-cache" / "docling" / f"{cache_key}.json"


def _load_cached_docling(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        from docling_core.types.doc import DoclingDocument

        document = DoclingDocument.model_validate(json.loads(path.read_text(encoding="utf-8")))
        os.utime(path, None)
        return document
    except Exception:
        path.unlink(missing_ok=True)
        return None


def _store_cached_docling(path: Path, document: Any) -> None:
    if not hasattr(document, "export_to_dict"):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document.export_to_dict(), ensure_ascii=False, separators=(",", ":"))
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as temporary:
        temporary.write(payload)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _prune_cache(cache_dir: Path, limit_bytes: int = 5 * 1024**3) -> None:
    files = sorted(
        (path for path in cache_dir.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    total = 0
    for path in files:
        size = path.stat().st_size
        total += size
        if total > limit_bytes:
            path.unlink(missing_ok=True)


def _deduplicate_citations(citations: list[Citation]) -> list[Citation]:
    unique: list[Citation] = []
    seen: set[tuple[str, tuple[int, ...], str]] = set()
    for citation in citations:
        key = (
            citation.logical_document_id or citation.document_id or "",
            tuple(citation.pages),
            " ".join(citation.excerpt.split()).casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation.model_copy(update={"retrieval_rank": len(unique) + 1}))
    return unique


@asynccontextmanager
async def _unguarded():
    yield


STRICT_PREAMBLE = """
Die bereitgestellten Fach- und Lehrbuecher sind die alleinige Wissensgrundlage.
Belege jede wesentliche fachliche Aussage mit den gelieferten Quellen. Erfinde
keine Seiten, Werte, Formeln, Normen oder Begruendungen. Wenn der Kontext nicht
ausreicht, antworte exakt: \"In den bereitgestellten Quellen nicht ausreichend
belegt.\" Uebernimm Zahlen, Einheiten und Formelzeichen exakt. Bei Konflikten
nenne beide Aussagen samt Ausgabe. Zitiere nur Originalquellen. Schreibe
mathematische Variablen und Formeln als LaTeX zwischen Dollarzeichen, zum
Beispiel $d_1$ oder $\\rho$. Gib relevante Tabellen vollstaendig als
Markdown-Tabelle wieder; vermische sie nicht mit benachbarten Tabellen. Fehlen
fuer eine verlangte Tabelle Zeilen oder Spalten im Kontext, behaupte keine
Vollstaendigkeit. Verweise im Fliesstext nicht auf Abbildungsnummern; die
Anwendung zeigt zugehoerige Bilder bei den Quellen.
""".strip()

NORMAL_PREAMBLE = """
Bevorzuge die bereitgestellten Fach- und Lehrbuecher und belege fachliche
Aussagen. Kennzeichne Schlussfolgerungen ausdruecklich. Wenn eine Aussage nicht
aus den Quellen folgt, sage das klar und erfinde keine Fundstelle. Gib relevante
Tabellen als Markdown und mathematische Ausdruecke als LaTeX zwischen
Dollarzeichen aus. Verweise nicht auf Abbildungsnummern im Fliesstext.
""".strip()

EXPLORE_PREAMBLE = """
Nutze die bereitgestellten Quellen als Ausgangspunkt. Trenne belegte Aussagen,
eigene Schlussfolgerungen und ergaenzendes Allgemeinwissen sichtbar voneinander.
Erfinde keine Fundstellen. Formatiere Tabellen als Markdown und Mathematik als
LaTeX zwischen Dollarzeichen.
""".strip()


def document_filter_for_ids(document_ids: list[str] | None) -> str | None:
    """Build a LanceDB-safe filter from IDs resolved by OmaRag's metadata store."""
    if document_ids is None:
        return None
    if not document_ids:
        return "id = '__omarag_no_document__'"
    quoted = ", ".join(f"'{value.replace(chr(39), chr(39) * 2)}'" for value in document_ids)
    return f"id IN ({quoted})"


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


def _chunk_manifest(chunk: Any, *, page_offset: int, segment_index: int) -> dict[str, Any]:
    metadata = dict(_value(chunk, "metadata", default={}) or {})
    content = str(_value(chunk, "content", default=""))
    pages = [page + page_offset for page in _int_list(metadata.get("page_numbers"))]
    return {
        "chunk_id": str(_value(chunk, "id", default="") or ""),
        "segment_index": segment_index,
        "chunk_order": int(_value(chunk, "order", default=0) or 0),
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "pages": pages,
        "headings": _string_list(metadata.get("headings")),
        "labels": _string_list(metadata.get("labels")),
        "doc_item_refs": _string_list(metadata.get("doc_item_refs")),
    }


def _content_chunks(chunks: list[Any]) -> list[Any]:
    excluded = {"page_header", "page_footer"}
    retained: list[Any] = []
    for chunk in chunks:
        labels = {
            value
            for item in _string_list((_value(chunk, "metadata", default={}) or {}).get("labels"))
            if (value := item.casefold().replace("-", "_").replace(" ", "_"))
        }
        if labels and labels <= excluded:
            continue
        retained.append(chunk)
    return retained


class VanillaHaikuAdapter(HaikuAdapter):
    """Compatibility boundary around Haiku RAG's documented public API."""

    name = "haiku-vanilla"

    def __init__(self) -> None:
        self.version = None
        for distribution in ("haiku-rag", "haiku-rag-slim"):
            try:
                self.version = metadata.version(distribution)
                break
            except metadata.PackageNotFoundError:
                continue
        # Installation is compatibility-gated by ``compat_probe``.  Do not
        # import Haiku merely to populate /meta: its client import pulls the
        # vector store and model stack into an otherwise idle API process.
        self._available = self.version is not None
        version_match = re.match(r"^(\d+)\.(\d+)", self.version or "")
        version_pair = (
            (int(version_match.group(1)), int(version_match.group(2)))
            if version_match is not None
            else (0, 0)
        )
        supports_images = self._available and version_pair >= (0, 72)
        self.capabilities = CapabilitySet(
            # OmaRag normalizes a completed public Haiku answer into deltas;
            # it does not expose Haiku/Pydantic-AI internal streaming events.
            streaming_chat=False,
            question_images=supports_images,
            analysis_images=supports_images,
            multimodal_search=False,
            multimodal_reranking=False,
            visual_grounding=supports_images,
            database_tags=False,
            native_ingester=False,
            evaluation=True,
        )

    @property
    def available(self) -> bool:
        return self._available

    @staticmethod
    def _client_type() -> type[Any]:
        try:
            from haiku.rag.client import HaikuRAG
        except ImportError as exc:
            raise AdapterUnavailableError(
                "Haiku RAG ist in der aktiven Python-Runtime nicht installiert",
                details={
                    "install_hint": "Installiere die zuletzt kompatibilitaetsgepruefte Version"
                },
            ) from exc
        return HaikuRAG

    @staticmethod
    def _config(database: Path) -> Any:
        try:
            import yaml
            from haiku.rag.config import AppConfig
        except ImportError as exc:
            raise AdapterUnavailableError(
                "Haiku RAG oder seine YAML-Laufzeit ist nicht vollstaendig installiert"
            ) from exc
        config_path = database.parent.parent / "haiku.rag.yaml"
        if not config_path.exists():
            return AppConfig()
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return AppConfig.model_validate(raw)

    def _client(self, database: Path, *, create: bool = False, config: Any | None = None) -> Any:
        return self._client_type()(
            database,
            config=config or self._config(database),
            create=create,
        )

    def _request_config(self, database: Path, evidence_mode: EvidenceMode) -> Any:
        config = copy.deepcopy(self._config(database))
        preamble = {
            EvidenceMode.STRICT: STRICT_PREAMBLE,
            EvidenceMode.NORMAL: NORMAL_PREAMBLE,
            EvidenceMode.EXPLORE: EXPLORE_PREAMBLE,
        }[evidence_mode]
        existing = str(config.prompts.domain_preamble or "").strip()
        config.prompts.domain_preamble = "\n\n".join(item for item in (existing, preamble) if item)
        config.qa.model.temperature = 0.1 if evidence_mode is EvidenceMode.STRICT else 0.2
        return config

    def _ask_supports_images(self) -> bool:
        if not self.available:
            return False
        try:
            return "images" in inspect.signature(self._client_type().ask).parameters
        except (TypeError, ValueError):
            return False

    def _analyze_supports_images(self) -> bool:
        if not self.available or not hasattr(self._client_type(), "analyze"):
            return False
        try:
            return "images" in inspect.signature(self._client_type().analyze).parameters
        except (TypeError, ValueError):
            return False

    @staticmethod
    def validate_config(content: str) -> None:
        try:
            import yaml
            from haiku.rag.config import AppConfig

            AppConfig.model_validate(yaml.safe_load(content) or {})
        except Exception as exc:
            raise ConflictError(
                "Haiku rejected this configuration",
                details={"validation_error": str(exc)},
            ) from exc

    async def ensure_database(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        if database.exists():
            return
        async with self._client(database, create=True):
            pass

    async def ingest(
        self,
        database: Path,
        source: str,
        *,
        parser_id: str = "auto",
        processing_profile: str = "default",
        segment_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        before_segment: Callable[[int, int, int], Awaitable[bool]] | None = None,
        generation_id: str | None = None,
        document_fingerprint: str | None = None,
        resume_segments: list[dict[str, Any]] | None = None,
        on_segment: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        segment_sizer: Callable[[int, bool], int] | None = None,
        metadata: BookMetadata | None = None,
        original_source: str | None = None,
    ) -> dict[str, Any]:
        if parser_id not in {"auto", "docling"}:
            raise ConflictError(f"Parser {parser_id} is not supported")
        await self.ensure_database(database)
        source_path = Path(source).expanduser()
        if source_path.is_file() and source_path.suffix.lower() == ".pdf":
            return await self._ingest_pdf_segments(
                database,
                source_path.resolve(),
                processing_profile=processing_profile,
                segment_guard=segment_guard,
                before_segment=before_segment,
                generation_id=generation_id,
                document_fingerprint=document_fingerprint,
                resume_segments=resume_segments,
                on_segment=on_segment,
                segment_sizer=segment_sizer,
                metadata=metadata,
                original_source=original_source,
            )
        guard = segment_guard or _unguarded
        async with guard(), self._client(database) as rag:
            document = await rag.create_document_from_source(
                source,
                title=metadata.title if metadata and metadata.title else None,
                metadata=_book_payload(metadata) or None,
            )
        return {
            "source": source,
            "document_id": str(_value(document, "id", "document_id", default="")),
            "book_metadata": metadata.model_dump(mode="json") if metadata else None,
            "original_source": original_source or source,
            "managed_source": source,
            "pipeline_version": "vanilla-haiku-public-api-v1",
        }

    async def _ingest_pdf_segments(
        self,
        database: Path,
        source: Path,
        *,
        processing_profile: str = "default",
        segment_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        before_segment: Callable[[int, int, int], Awaitable[bool]] | None = None,
        generation_id: str | None = None,
        document_fingerprint: str | None = None,
        resume_segments: list[dict[str, Any]] | None = None,
        on_segment: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        segment_sizer: Callable[[int, bool], int] | None = None,
        metadata: BookMetadata | None = None,
        original_source: str | None = None,
    ) -> dict[str, Any]:
        book_metadata = metadata
        document_fingerprint = document_fingerprint or await asyncio.to_thread(_file_sha256, source)
        total_pages, scanned_pages = await asyncio.to_thread(
            _cached_pdf_info,
            source,
            _cache_file(database, f"profile-{document_fingerprint}"),
        )
        if isinstance(scanned_pages, bool):
            scanned_pages = [scanned_pages] * total_pages
        if total_pages == 0:
            raise ConflictError(f"PDF {source.name} contains no pages")
        logical_id = f"book-{document_fingerprint[:20]}"
        generation_id = generation_id or f"gen-{uuid4().hex[:16]}"
        source_uri = source.as_uri()
        segment_sizes = {
            "eco": (12, 5),
            "low-memory": (10, 4),
            "default": (25, 10),
            "technical": (25, 10),
            "balanced": (25, 10),
            "quality": (16, 6),
            "image-heavy": (8, 3),
            "fast": (40, 15),
        }
        text_size, scan_size = segment_sizes.get(processing_profile, segment_sizes["default"])
        guard = segment_guard or _unguarded
        resume_segments = sorted(resume_segments or [], key=lambda item: item["segment_index"])
        # Resume positions must be derived from segments that still exist in Haiku.
        # A persisted checkpoint can outlive its imported document after cleanup.
        start = 0
        segment_index = 0
        imported_ids = [str(item["document_id"]) for item in resume_segments]
        segments = [dict(item) for item in resume_segments]
        cache_hits = sum(bool(item.get("metadata", {}).get("cache_hit")) for item in segments)
        converted_segments = 0
        manifest_chunks: list[dict[str, Any]] = []
        quality_counts = {"chunks": 0, "tables": 0, "formulas": 0, "pictures": 0, "refs": 0}
        base_config = self._config(database)
        async with self._client(database) as rag:
            listed_documents = await rag.list_documents()
        current_ids = {str(document.id) for document in listed_documents if document.id}
        imported_ids = [document_id for document_id in imported_ids if document_id in current_ids]
        segments = [item for item in segments if str(item["document_id"]) in current_ids]
        for segment in segments:
            for item in segment.get("metadata", {}).get("chunk_manifest", []):
                labels = {label.casefold() for label in item.get("labels", [])}
                quality_counts["chunks"] += 1
                quality_counts["tables"] += bool(labels & {"table", "table_item"})
                quality_counts["formulas"] += bool(labels & {"formula", "equation"})
                quality_counts["pictures"] += bool(labels & {"picture", "figure", "image"})
                quality_counts["refs"] += bool(item.get("doc_item_refs"))
                manifest_chunks.append(item)
        generation_documents = [
            document
            for document in listed_documents
            if document.id
            and (_value(document, "metadata", default={}) or {}).get("generation_id")
            == generation_id
        ]
        known_ids = set(imported_ids)
        for document in generation_documents:
            document_id = str(document.id)
            if document_id in known_ids:
                continue
            metadata_value = _value(document, "metadata", default={}) or {}
            recovered = {
                "document_id": document_id,
                "segment_index": int(metadata_value.get("segment_index", len(segments))),
                "page_start": int(metadata_value.get("page_start", 1)),
                "page_end": int(metadata_value.get("page_end", 1)),
                "fingerprint": document_fingerprint,
                "generation_id": generation_id,
                "status": "committed",
                "metadata": {
                    "recovered": True,
                    "cache_hit": True,
                    "page_kind": metadata_value.get("page_kind"),
                },
            }
            segments.append(recovered)
            imported_ids.append(document_id)
            known_ids.add(document_id)
            if on_segment is not None:
                await on_segment(recovered)
        segments.sort(key=lambda item: item["segment_index"])
        if segments:
            last_page = int(segments[-1]["page_end"])
            last_kind = segments[-1].get("metadata", {}).get("page_kind")
            next_kind = (
                "scanned" if last_page < total_pages and scanned_pages[last_page] else "text"
            )
            start = (
                total_pages
                if last_page >= total_pages
                else last_page
                if last_kind and last_kind != next_kind
                else max(0, last_page - 1)
            )
            segment_index = int(segments[-1]["segment_index"]) + 1
        old_document_ids = [
            str(document.id)
            for document in listed_documents
            if document.id
            and (_value(document, "metadata", default={}) or {}).get("logical_document_id")
            == logical_id
            and (_value(document, "metadata", default={}) or {}).get("generation_id")
            != generation_id
        ]
        while start < total_pages:
            scanned = scanned_pages[start]
            segment_size = scan_size if scanned else text_size
            if segment_sizer is not None:
                segment_size = max(1, segment_sizer(segment_size, scanned))
            end = min(start + segment_size, total_pages)
            kind_boundary = False
            # Keep OCR and native-text pages in separate conversions.
            for boundary in range(start + 1, end):
                if scanned_pages[boundary] != scanned:
                    end = boundary
                    kind_boundary = True
                    break
            if before_segment is not None and not await before_segment(start, end, total_pages):
                raise asyncio.CancelledError
            try:
                async with guard():
                    pdf_bytes = await asyncio.to_thread(_pdf_slice, source, start, end)
                    with tempfile.NamedTemporaryFile(
                        mode="wb", suffix=".pdf", delete=False
                    ) as temporary:
                        temporary.write(pdf_bytes)
                        temporary_path = Path(temporary.name)
                    try:
                        config = copy.deepcopy(base_config)
                        options = config.processing.conversion_options
                        # A page can contain plenty of native text and still keep
                        # its most important table or formula as a bitmap.  The
                        # page classifier only decides segmentation; it must not
                        # silently disable the OCR policy selected by the user.
                        options.do_ocr = bool(options.do_ocr) or scanned
                        options.force_ocr = bool(options.force_ocr)
                        if processing_profile in {"eco", "low-memory", "fast"}:
                            # These opt-in profiles trade table reconstruction
                            # detail for a lower Docling peak. Technical and
                            # quality profiles retain accurate table handling.
                            options.table_mode = "fast"
                            options.table_cell_matching = False
                            options.images_scale = min(float(options.images_scale), 1.0)
                        conversion_signature = {
                            "do_ocr": bool(options.do_ocr),
                            "force_ocr": bool(options.force_ocr),
                            "ocr_engine": str(getattr(options, "ocr_engine", "")),
                            "ocr_lang": list(getattr(options, "ocr_lang", []) or []),
                            "do_table_structure": bool(
                                getattr(options, "do_table_structure", False)
                            ),
                            "table_mode": str(getattr(options, "table_mode", "")),
                            "table_cell_matching": bool(
                                getattr(options, "table_cell_matching", False)
                            ),
                            "images_scale": float(getattr(options, "images_scale", 1.0)),
                        }
                        cache_material = (
                            f"v3:{document_fingerprint}:{start}:{end}:{processing_profile}:"
                            f"{json.dumps(conversion_signature, sort_keys=True)}:"
                            f"haiku={self.version or 'unknown'}"
                        )
                        cache_key = hashlib.sha256(cache_material.encode()).hexdigest()
                        cache_path = _cache_file(database, cache_key)
                        docling_document = await asyncio.to_thread(_load_cached_docling, cache_path)
                        cache_hit = docling_document is not None
                        async with self._client(database, config=config) as segment_rag:
                            if docling_document is None:
                                docling_document = await segment_rag.convert(
                                    temporary_path, source_uri=source_uri
                                )
                                await asyncio.to_thread(
                                    _store_cached_docling, cache_path, docling_document
                                )
                                converted_segments += 1
                            else:
                                cache_hits += 1
                            chunks = _content_chunks(await segment_rag.chunk(docling_document))
                            document_metadata = {
                                "logical_document_id": logical_id,
                                "generation_id": generation_id,
                                "segment_index": segment_index,
                                "page_start": start + 1,
                                "page_end": end,
                                "page_offset": start,
                                "source_uri": source_uri,
                                "parser_id": "docling",
                                "processing_profile": processing_profile,
                                "chunker": "hybrid",
                                "fingerprint": document_fingerprint,
                                "page_kind": "scanned" if scanned else "text",
                                "cache_key": cache_key,
                                **_book_payload(book_metadata),
                            }
                            segment_uri = (
                                f"{source_uri}#oracle-pages={start + 1}-{end}"
                                f"&generation={generation_id}"
                            )
                            document = await segment_rag.import_document(
                                docling_document,
                                chunks,
                                uri=segment_uri,
                                title=(document_metadata.get("book_title") or source.name),
                                metadata=document_metadata,
                            )
                            segment_manifest = [
                                _chunk_manifest(
                                    chunk,
                                    page_offset=start,
                                    segment_index=segment_index,
                                )
                                for chunk in chunks
                            ]
                            for item in segment_manifest:
                                labels = {label.casefold() for label in item["labels"]}
                                quality_counts["chunks"] += 1
                                quality_counts["tables"] += bool(labels & {"table", "table_item"})
                                quality_counts["formulas"] += bool(labels & {"formula", "equation"})
                                quality_counts["pictures"] += bool(
                                    labels & {"picture", "figure", "image"}
                                )
                                quality_counts["refs"] += bool(item["doc_item_refs"])
                            manifest_chunks.extend(segment_manifest)
                    finally:
                        temporary_path.unlink(missing_ok=True)
            except Exception as exc:
                if segment_size > 1 and _looks_like_memory_pressure(exc):
                    text_size = max(1, text_size // 2)
                    scan_size = max(1, scan_size // 2)
                    continue
                raise
            document_id = str(_value(document, "id", "document_id", default=""))
            imported_ids.append(document_id)
            segment = {
                "document_id": document_id,
                "segment_index": segment_index,
                "page_start": start + 1,
                "page_end": end,
                "fingerprint": document_fingerprint,
                "generation_id": generation_id,
                "status": "committed",
                "metadata": {
                    "cache_hit": cache_hit,
                    "cache_key": cache_key,
                    "cache_path": str(cache_path),
                    "page_kind": "scanned" if scanned else "text",
                    "chunk_manifest": segment_manifest,
                },
            }
            segments.append(segment)
            if on_segment is not None:
                await on_segment(segment)
            segment_index += 1
            if end == total_pages:
                break
            # Keep one boundary page in both segments. Citation
            # normalization and adapter-level deduplication hide the
            # overlap while preserving long tables and sections.
            start = end if kind_boundary else max(start + 1, end - 1)
        # Only retire the previous generation after every new segment is
        # searchable. Interrupted work remains staged and can be resumed.
        async with self._client(database) as rag:
            for document_id in old_document_ids:
                with suppress(Exception):
                    await rag.delete_document(document_id)
        await asyncio.to_thread(_prune_cache, _cache_file(database, "x").parent)
        provenance_coverage = quality_counts["refs"] / max(quality_counts["chunks"], 1)
        quality = DocumentQuality(
            score=max(0.0, min(1.0, 0.75 + 0.25 * provenance_coverage)),
            pages_total=total_pages,
            native_text_pages=total_pages - sum(scanned_pages),
            ocr_pages=sum(scanned_pages),
            chunks=quality_counts["chunks"],
            tables=quality_counts["tables"],
            formulas=quality_counts["formulas"],
            pictures=quality_counts["pictures"],
            provenance_coverage=provenance_coverage,
            issues=(
                ["Ein Teil der Chunks besitzt keine elementgenaue PDF-Provenienz."]
                if provenance_coverage < 0.9
                else []
            ),
        )
        return {
            "source": str(source),
            "source_uri": source_uri,
            "document_id": logical_id,
            "logical_document_id": logical_id,
            "generation_id": generation_id,
            "segment_document_ids": imported_ids,
            "segments": segments,
            "page_count": total_pages,
            "scanned": any(scanned_pages),
            "scanned_pages": sum(scanned_pages),
            "parser_id": "docling",
            "processing_profile": processing_profile,
            "fingerprint": document_fingerprint,
            "book_metadata": (book_metadata.model_dump(mode="json") if book_metadata else None),
            "original_source": original_source or str(source),
            "managed_source": str(source),
            "pipeline_version": "vanilla-haiku-public-api-v1",
            "quality": quality.model_dump(mode="json"),
            "chunk_manifest": manifest_chunks,
            "cache_status": (
                "hit" if converted_segments == 0 else "miss" if cache_hits == 0 else "mixed"
            ),
            "pipeline_stats": {
                "cache_hits": cache_hits,
                "converted_segments": converted_segments,
                "ocr_pages": sum(scanned_pages),
                "text_pages": total_pages - sum(scanned_pages),
                "vl_pages": 0,
            },
        }

    async def delete_document(self, database: Path, document_id: str) -> bool:
        await self.ensure_database(database)
        async with self._client(database) as rag:
            result = await rag.delete_document(document_id)
        return bool(result)

    async def search(
        self,
        database: Path,
        query: str,
        limit: int,
        *,
        document_filter: str | None = None,
        search_type: str = "hybrid",
    ) -> list[SearchHit]:
        await self.ensure_database(database)
        async with self._client(database) as rag:
            results = await rag.search(
                query,
                limit=limit,
                search_type=search_type,
                filter=document_filter,
            )
        hits: list[SearchHit] = []
        for index, result in enumerate(results):
            pages = _int_list(_value(result, "page_numbers", "pages", default=[]))
            metadata = dict(_value(result, "metadata", default={}) or {})
            metadata.update(
                {
                    "source_uri": _value(result, "document_uri"),
                    "headings": _string_list(_value(result, "headings")),
                    "labels": _string_list(_value(result, "labels")),
                    "doc_item_refs": _string_list(_value(result, "doc_item_refs")),
                    "chunk_ids": _string_list(_value(result, "chunk_ids")),
                }
            )
            hits.append(
                SearchHit(
                    chunk_id=str(_value(result, "chunk_id", "id", default=f"chunk-{index}")),
                    content=str(_value(result, "content", "text", default="")),
                    score=_value(result, "score"),
                    pages=list(pages),
                    document_id=_value(result, "document_id"),
                    document_title=_value(result, "document_title", "title"),
                    metadata=metadata,
                    search_type=search_type,
                )
            )
        return hits

    async def _citation(self, rag: Any, cite: Any, index: int) -> Citation:
        chunk_id = str(_value(cite, "chunk_id", "id", default=f"chunk-{index}"))
        document_id = _value(cite, "document_id")
        document_meta = dict(_value(cite, "document_meta", default={}) or {})
        raw_book = document_meta.get("book_metadata")
        try:
            book = BookMetadata.model_validate(raw_book) if raw_book else None
        except Exception:
            book = None
        page_offset = int(document_meta.get("page_offset", 0) or 0)
        logical_id = document_meta.get("logical_document_id") or document_id
        pages = [page + page_offset for page in _int_list(_value(cite, "page_numbers", "pages"))]
        all_refs = _string_list(_value(cite, "doc_item_refs"))
        chunk_ids = _string_list(_value(cite, "chunk_ids")) or [chunk_id]
        primary_refs: list[str] = []
        if chunk_id:
            try:
                primary_chunk = await rag.get_chunk_by_id(chunk_id)
                if primary_chunk is not None:
                    primary_refs = _string_list(
                        _value(primary_chunk, "metadata", default={}).get("doc_item_refs", [])
                    )
            except Exception:
                # Citation display must keep working with old/partial stores.
                primary_refs = []
        primary_set = set(primary_refs)
        context_refs = [ref for ref in all_refs if ref not in primary_set]
        document = None
        if document_id:
            try:
                stored = await rag.get_document_by_id(document_id)
                document = stored.get_docling_document() if stored is not None else None
            except Exception:
                document = None
        primary_anchors = (
            _anchors_for_refs(document, primary_refs or all_refs, page_offset=page_offset)
            if document is not None
            else []
        )
        context_anchors = (
            _anchors_for_refs(document, context_refs, page_offset=page_offset)
            if document is not None
            else []
        )
        element_types = list(
            dict.fromkeys(
                anchor.element_type
                for anchor in [*primary_anchors, *context_anchors]
                if anchor.element_type
            )
        )
        if not element_types:
            element_types = _string_list(_value(cite, "labels"))
        return Citation(
            chunk_id=chunk_id,
            chunk_ids=chunk_ids,
            document_id=document_id,
            logical_document_id=str(logical_id) if logical_id else None,
            source_uri=document_meta.get("source_uri")
            or _value(cite, "document_uri", "source_uri"),
            document_title=_value(cite, "document_title", "title"),
            pages=list(dict.fromkeys(pages)),
            headings=_string_list(_value(cite, "headings")),
            element_types=element_types,
            doc_item_refs=all_refs,
            picture_refs=_string_list(_value(cite, "picture_refs")),
            primary_anchors=primary_anchors,
            context_anchors=context_anchors,
            excerpt=str(_value(cite, "content", "excerpt", default="")),
            retrieval_rank=index + 1,
            rerank_score=_value(cite, "score", "rerank_score"),
            book=book,
            verification_status="provider-grounded",
        )

    async def ask(
        self,
        database: Path,
        question: str,
        images: list[str] | None = None,
        *,
        document_filter: str | None = None,
        evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    ) -> tuple[str, list[Citation]]:
        await self.ensure_database(database)
        kwargs: dict[str, Any] = {}
        if images:
            if not self._ask_supports_images():
                raise AdapterUnavailableError(
                    "Die aktive Haiku-Runtime unterstuetzt keine Bildanhaenge"
                )
            kwargs["images"] = [Path(image).read_bytes() for image in images]
        kwargs["filter"] = document_filter
        async with self._client(
            database, config=self._request_config(database, evidence_mode)
        ) as rag:
            answer, raw_citations = await rag.ask(question, **kwargs)
            citations = _deduplicate_citations(
                [await self._citation(rag, cite, index) for index, cite in enumerate(raw_citations)]
            )
        return str(answer), citations

    async def analyze(
        self,
        database: Path,
        question: str,
        images: list[str] | None = None,
        *,
        document_filter: str | None = None,
        evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    ) -> tuple[str, list[Citation]]:
        await self.ensure_database(database)
        kwargs: dict[str, Any] = {}
        if images:
            if not self._analyze_supports_images():
                raise AdapterUnavailableError(
                    "The active Haiku runtime does not support analysis images"
                )
            kwargs["images"] = [Path(image).read_bytes() for image in images]
        kwargs["filter"] = document_filter
        async with self._client(
            database, config=self._request_config(database, evidence_mode)
        ) as rag:
            result = await rag.analyze(question, **kwargs)
            answer = _value(result, "answer", default="")
            raw_citations = _value(result, "citations", default=[]) or []
            citations = _deduplicate_citations(
                [await self._citation(rag, cite, index) for index, cite in enumerate(raw_citations)]
            )
        return str(answer), citations

    async def update_document_metadata(
        self,
        database: Path,
        document_ids: list[str],
        metadata: dict[str, Any],
    ) -> None:
        await self.ensure_database(database)
        async with self._client(database) as rag:
            for document_id in document_ids:
                document = await rag.get_document_by_id(document_id)
                if document is None:
                    continue
                current = dict(_value(document, "metadata", default={}) or {})
                current.update(_book_payload(BookMetadata.model_validate(metadata)))
                await rag.update_document(
                    document_id,
                    metadata=current,
                    title=metadata.get("title") or _value(document, "title"),
                )


# Kept as an import alias for third-party clients and existing test fixtures.
HaikuV070Adapter = VanillaHaikuAdapter
