from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from importlib import metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..models.domain import CapabilitySet, Citation, CitationAnchor, SearchHit
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


def _pdf_info(path: Path) -> tuple[int, bool]:
    """Return page count and a cheap text-vs-scan classification."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        total = len(document)
        sample_chars = 0
        sample_count = min(total, 3)
        for index in range(sample_count):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    sample_chars += len(text_page.get_text_range())
                finally:
                    text_page.close()
            finally:
                page.close()
        scanned = sample_count > 0 and sample_chars / sample_count < 100
        return total, scanned
    finally:
        document.close()


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


class HaikuV070Adapter(HaikuAdapter):
    name = "haiku-v070"

    def __init__(self) -> None:
        try:
            self.version = metadata.version("haiku-rag")
        except metadata.PackageNotFoundError:
            try:
                self.version = metadata.version("haiku.rag")
            except metadata.PackageNotFoundError:
                self.version = None
        self.capabilities = CapabilitySet(
            # OmaRag normalizes a completed public Haiku answer into deltas;
            # it does not expose Haiku/Pydantic-AI internal streaming events.
            streaming_chat=False,
            question_images=self.available and self._ask_supports_images(),
            analysis_images=self.available and self._analyze_supports_images(),
            multimodal_search=False,
            multimodal_reranking=False,
            visual_grounding=self.available and self._ask_supports_images(),
            database_tags=False,
            native_ingester=False,
            evaluation=False,
        )

    @property
    def available(self) -> bool:
        try:
            from haiku.rag.client import HaikuRAG  # noqa: F401

            return True
        except ImportError:
            return False

    @staticmethod
    def _client_type() -> type[Any]:
        try:
            from haiku.rag.client import HaikuRAG
        except ImportError as exc:
            raise AdapterUnavailableError(
                "Haiku RAG ist in der aktiven Python-Runtime nicht installiert",
                details={"install_hint": "Installiere eine freigegebene Haiku-RAG-0.70.x-Runtime"},
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

    def _client(self, database: Path, *, create: bool = False) -> Any:
        return self._client_type()(
            database,
            config=self._config(database),
            create=create,
        )

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
            )
        guard = segment_guard or _unguarded
        async with guard(), self._client(database) as rag:
            document = await rag.create_document_from_source(source)
        return {
            "source": source,
            "document_id": str(_value(document, "id", "document_id", default="")),
        }

    async def _ingest_pdf_segments(
        self,
        database: Path,
        source: Path,
        *,
        processing_profile: str = "default",
        segment_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        before_segment: Callable[[int, int, int], Awaitable[bool]] | None = None,
    ) -> dict[str, Any]:
        total_pages, scanned = await asyncio.to_thread(_pdf_info, source)
        if total_pages == 0:
            raise ConflictError(f"PDF {source.name} contains no pages")
        logical_id = f"book-{hashlib.sha256(str(source).encode()).hexdigest()[:20]}"
        generation_id = f"gen-{uuid4().hex[:16]}"
        source_uri = source.as_uri()
        segment_sizes = {
            "eco": (12, 5),
            "default": (25, 10),
            "technical": (25, 10),
            "balanced": (25, 10),
            "fast": (40, 15),
        }
        text_size, scan_size = segment_sizes.get(processing_profile, segment_sizes["default"])
        segment_size = scan_size if scanned else text_size
        guard = segment_guard or _unguarded
        start = 0
        segment_index = 0
        imported_ids: list[str] = []
        segments: list[dict[str, Any]] = []
        async with self._client(database) as rag:
            old_document_ids = [
                str(document.id)
                for document in await rag.list_documents()
                if document.id
                and (_value(document, "metadata", default={}) or {}).get("logical_document_id")
                == logical_id
            ]
            try:
                while start < total_pages:
                    end = min(start + segment_size, total_pages)
                    if before_segment is not None and not await before_segment(
                        start, end, total_pages
                    ):
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
                                docling_document = await rag.convert(
                                    temporary_path, source_uri=source_uri
                                )
                                chunks = await rag.chunk(docling_document)
                                metadata = {
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
                                }
                                segment_uri = (
                                    f"{source_uri}#oracle-pages={start + 1}-{end}"
                                    f"&generation={generation_id}"
                                )
                                document = await rag.import_document(
                                    docling_document,
                                    chunks,
                                    uri=segment_uri,
                                    title=source.name,
                                    metadata=metadata,
                                )
                            finally:
                                temporary_path.unlink(missing_ok=True)
                    except Exception as exc:
                        if segment_size > 1 and _looks_like_memory_pressure(exc):
                            segment_size = max(1, segment_size // 2)
                            continue
                        raise
                    document_id = str(_value(document, "id", "document_id", default=""))
                    imported_ids.append(document_id)
                    segments.append(
                        {
                            "document_id": document_id,
                            "segment_index": segment_index,
                            "page_start": start + 1,
                            "page_end": end,
                        }
                    )
                    segment_index += 1
                    if end == total_pages:
                        break
                    # Keep one boundary page in both segments. Citation
                    # normalization and adapter-level deduplication hide the
                    # overlap while preserving long tables and sections.
                    start = max(start + 1, end - 1)
            except Exception:
                for document_id in reversed(imported_ids):
                    with suppress(Exception):
                        await rag.delete_document(document_id)
                raise
            # Only retire the previous generation after every new segment is
            # searchable. A failed update therefore leaves the old book intact.
            for document_id in old_document_ids:
                with suppress(Exception):
                    await rag.delete_document(document_id)
        return {
            "source": str(source),
            "source_uri": source_uri,
            "document_id": logical_id,
            "logical_document_id": logical_id,
            "generation_id": generation_id,
            "segment_document_ids": imported_ids,
            "segments": segments,
            "page_count": total_pages,
            "scanned": scanned,
            "parser_id": "docling",
            "processing_profile": processing_profile,
        }

    async def delete_document(self, database: Path, document_id: str) -> bool:
        await self.ensure_database(database)
        async with self._client(database) as rag:
            result = await rag.delete_document(document_id)
        return bool(result)

    async def search(self, database: Path, query: str, limit: int) -> list[SearchHit]:
        await self.ensure_database(database)
        async with self._client(database) as rag:
            results = await rag.search(query, limit=limit)
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
                )
            )
        return hits

    async def _citation(self, rag: Any, cite: Any, index: int) -> Citation:
        chunk_id = str(_value(cite, "chunk_id", "id", default=f"chunk-{index}"))
        document_id = _value(cite, "document_id")
        document_meta = dict(_value(cite, "document_meta", default={}) or {})
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
        )

    async def ask(
        self, database: Path, question: str, images: list[str] | None = None
    ) -> tuple[str, list[Citation]]:
        await self.ensure_database(database)
        kwargs: dict[str, Any] = {}
        if images:
            if not self._ask_supports_images():
                raise AdapterUnavailableError(
                    "Die aktive Haiku-Runtime unterstuetzt keine Bildanhaenge"
                )
            kwargs["images"] = [Path(image).read_bytes() for image in images]
        async with self._client(database) as rag:
            answer, raw_citations = await rag.ask(question, **kwargs)
            citations = _deduplicate_citations(
                [await self._citation(rag, cite, index) for index, cite in enumerate(raw_citations)]
            )
        return str(answer), citations

    async def analyze(
        self, database: Path, question: str, images: list[str] | None = None
    ) -> tuple[str, list[Citation]]:
        await self.ensure_database(database)
        kwargs: dict[str, Any] = {}
        if images:
            if not self._analyze_supports_images():
                raise AdapterUnavailableError(
                    "The active Haiku runtime does not support analysis images"
                )
            kwargs["images"] = [Path(image).read_bytes() for image in images]
        async with self._client(database) as rag:
            result = await rag.analyze(question, **kwargs)
            answer = _value(result, "answer", default="")
            raw_citations = _value(result, "citations", default=[]) or []
            citations = _deduplicate_citations(
                [await self._citation(rag, cite, index) for index, cite in enumerate(raw_citations)]
            )
        return str(answer), citations
