from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from importlib import metadata
from pathlib import Path
from typing import Any

from ..models.domain import (
    BookMetadata,
    CapabilitySet,
    Citation,
    CitationAnchor,
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


def _absolute_pages(pages: Any, document_meta: dict[str, Any]) -> list[int]:
    values = _int_list(pages)
    if document_meta.get("page_number_mode") == "absolute" or document_meta.get("pages_absolute"):
        return values
    offset = int(document_meta.get("page_offset", 0) or 0)
    return [page + offset for page in values]


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


class VanillaHaikuAdapter(HaikuAdapter):
    """Compatibility boundary around Haiku RAG's documented public API."""

    name = "haiku-vanilla"

    def __init__(self) -> None:
        self._persistent_reranker: Any | None = None
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
            streaming_chat=True,
            question_images=supports_images,
            analysis_images=supports_images,
            multimodal_search=False,
            multimodal_reranking=False,
            visual_grounding=supports_images,
            database_tags=False,
            native_ingester=False,
            evaluation=True,
            book_index_v2=True,
            adaptive_retrieval=True,
            claim_streaming=True,
            knowledge_snapshots=True,
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
        # Pydantic-AI cannot infer a useful output budget for local Ollama
        # models.  In particular, Qwen 3.5 may otherwise spend the provider
        # default entirely on hidden reasoning and return no answer text.
        if config.qa.model.max_tokens is None:
            config.qa.model.max_tokens = 1024
        if config.qa.model.provider == "ollama" and config.qa.model.enable_thinking is False:
            extra_body = dict(config.qa.model.extra_body or {})
            # Ollama's OpenAI-compatible endpoint maps this field to its native
            # ``think: false`` behavior.  Haiku's generic enable_thinking flag
            # is intentionally not mapped on this provider path.
            extra_body["reasoning_effort"] = "none"
            config.qa.model.extra_body = extra_body
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

    async def warm(self, database: Path) -> None:
        await self.ensure_database(database)
        async with self._client(database) as rag:
            await rag.list_documents()

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
        on_phase: Callable[[str, int, int, int], Awaitable[None]] | None = None,
        segment_sizer: Callable[[int, bool], int] | None = None,
        metadata: BookMetadata | None = None,
        original_source: str | None = None,
        indexing_options: dict[str, Any] | None = None,
        llm_url: str | None = None,
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
                on_phase=on_phase,
                segment_sizer=segment_sizer,
                metadata=metadata,
                original_source=original_source,
                indexing_options=indexing_options,
                llm_url=llm_url,
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
        on_phase: Callable[[str, int, int, int], Awaitable[None]] | None = None,
        segment_sizer: Callable[[int, bool], int] | None = None,
        metadata: BookMetadata | None = None,
        original_source: str | None = None,
        indexing_options: dict[str, Any] | None = None,
        llm_url: str | None = None,
    ) -> dict[str, Any]:
        from .book_v2 import ingest_pdf_book_v2

        return await ingest_pdf_book_v2(
            database=database,
            source=source,
            config=self._config(database),
            client_factory=self._client,
            haiku_version=self.version,
            processing_profile=processing_profile,
            segment_guard=segment_guard,
            before_segment=before_segment,
            generation_id=generation_id,
            document_fingerprint=document_fingerprint,
            resume_segments=resume_segments,
            on_segment=on_segment,
            on_phase=on_phase,
            segment_sizer=segment_sizer,
            metadata=metadata,
            original_source=original_source,
            indexing_options=indexing_options,
            llm_url=llm_url,
        )

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
        rerank: bool = True,
    ) -> list[SearchHit]:
        await self.ensure_database(database)
        config = None
        if not rerank:
            config = copy.deepcopy(self._config(database))
            config.reranking.model = None
        async with self._client(database, config=config) as rag:
            search_kwargs: dict[str, Any] = {
                "limit": limit,
                "search_type": search_type,
                "filter": document_filter,
            }
            if "include_images" in inspect.signature(rag.search).parameters:
                search_kwargs["include_images"] = False
            results = await rag.search(query, **search_kwargs)
            hits: list[SearchHit] = []
            result_rows = list(results)
            chunk_ids = [
                str(_value(result, "chunk_id", "id", default=f"chunk-{index}"))
                for index, result in enumerate(result_rows)
            ]

            async def optional(call: Any) -> Any:
                try:
                    return await call
                except Exception:
                    return None

            stored_chunks = await asyncio.gather(
                *(optional(rag.get_chunk_by_id(chunk_id)) for chunk_id in chunk_ids)
            )
            document_ids = list(
                dict.fromkeys(
                    str(document_id)
                    for result, stored_chunk in zip(result_rows, stored_chunks, strict=True)
                    if (
                        document_id := _value(result, "document_id")
                        or _value(stored_chunk, "document_id")
                    )
                    and not dict(_value(result, "document_meta", default={}) or {})
                )
            )
            document_rows = await asyncio.gather(
                *(optional(rag.get_document_by_id(document_id)) for document_id in document_ids)
            )
            document_cache = dict(zip(document_ids, document_rows, strict=True))
            for result, result_chunk_id, stored_chunk in zip(
                result_rows, chunk_ids, stored_chunks, strict=True
            ):
                chunk_metadata = dict(_value(stored_chunk, "metadata", default={}) or {})
                result_metadata = dict(_value(result, "metadata", default={}) or {})
                document_id = _value(result, "document_id") or _value(stored_chunk, "document_id")
                document = None
                if document_id:
                    key = str(document_id)
                    document = document_cache.get(key)
                document_meta = dict(_value(document, "metadata", default={}) or {})
                document_meta.update(dict(_value(stored_chunk, "document_meta", default={}) or {}))
                document_meta.update(dict(_value(result, "document_meta", default={}) or {}))
                pages = _absolute_pages(
                    chunk_metadata.get("page_numbers")
                    or _value(result, "page_numbers", "pages", default=[]),
                    document_meta,
                )
                citation_headings = _string_list(
                    chunk_metadata.get("citation_headings") or _value(result, "headings")
                )
                metadata = {
                    **result_metadata,
                    **chunk_metadata,
                    "source_uri": _value(result, "document_uri") or _value(document, "uri"),
                    "headings": citation_headings,
                    "labels": _string_list(
                        chunk_metadata.get("labels") or _value(result, "labels")
                    ),
                    "doc_item_refs": _string_list(
                        chunk_metadata.get("doc_item_refs") or _value(result, "doc_item_refs")
                    ),
                    "chunk_ids": _string_list(_value(result, "chunk_ids")),
                    "document_meta": document_meta,
                    "logical_document_id": document_meta.get("logical_document_id"),
                    "generation_id": document_meta.get("generation_id"),
                    "raw_evidence": True,
                }
                hits.append(
                    SearchHit(
                        chunk_id=result_chunk_id,
                        content=str(_value(result, "content", "text", default="")),
                        score=_value(result, "score"),
                        pages=pages,
                        document_id=document_id,
                        document_title=_value(result, "document_title", "title")
                        or _value(document, "title"),
                        metadata=metadata,
                        search_type=search_type,
                    )
                )
        return hits

    async def get_chunk(self, database: Path, chunk_id: str) -> SearchHit | None:
        """Return unexpanded raw evidence via Haiku's public chunk lookup."""
        await self.ensure_database(database)
        async with self._client(database) as rag:
            chunk = await rag.get_chunk_by_id(chunk_id)
            document_id = _value(chunk, "document_id") if chunk is not None else None
            document = None
            if document_id:
                with suppress(Exception):
                    document = await rag.get_document_by_id(str(document_id))
        if chunk is None:
            return None
        chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
        document_meta = dict(
            _value(document, "metadata", default=None)
            or _value(chunk, "document_meta", default={})
            or {}
        )
        metadata_payload = {
            **chunk_metadata,
            "source_uri": _value(chunk, "document_uri") or _value(document, "uri"),
            "document_meta": document_meta,
            "logical_document_id": document_meta.get("logical_document_id"),
            "generation_id": document_meta.get("generation_id"),
            "raw_evidence": True,
        }
        return SearchHit(
            chunk_id=str(_value(chunk, "id", default=chunk_id) or chunk_id),
            content=str(_value(chunk, "content", default="")),
            pages=_absolute_pages(chunk_metadata.get("page_numbers"), document_meta),
            document_id=_value(chunk, "document_id"),
            document_title=_value(chunk, "document_title") or _value(document, "title"),
            metadata=metadata_payload,
            search_type="lookup",
        )

    async def get_chunks(self, database: Path, chunk_ids: list[str]) -> list[SearchHit]:
        """Resolve a bounded batch while reusing one public Haiku client."""
        if not chunk_ids:
            return []
        await self.ensure_database(database)
        async with self._client(database) as rag:
            unique_chunk_ids = list(dict.fromkeys(chunk_ids))

            async def optional(call: Any) -> Any:
                try:
                    return await call
                except Exception:
                    return None

            chunk_rows = await asyncio.gather(
                *(optional(rag.get_chunk_by_id(chunk_id)) for chunk_id in unique_chunk_ids)
            )
            chunks = [
                (chunk_id, chunk)
                for chunk_id, chunk in zip(unique_chunk_ids, chunk_rows, strict=True)
                if chunk is not None
            ]
            document_ids = list(
                dict.fromkeys(
                    document_id
                    for _chunk_id, chunk in chunks
                    if (document_id := str(_value(chunk, "document_id", default="") or ""))
                )
            )
            document_rows = await asyncio.gather(
                *(optional(rag.get_document_by_id(document_id)) for document_id in document_ids)
            )
            documents = dict(zip(document_ids, document_rows, strict=True))
            resolved: list[SearchHit] = []
            for chunk_id, chunk in chunks:
                chunk_metadata = dict(_value(chunk, "metadata", default={}) or {})
                document_id = str(_value(chunk, "document_id", default="") or "")
                document = documents.get(document_id)
                document_meta = dict(
                    _value(document, "metadata", default=None)
                    or _value(chunk, "document_meta", default={})
                    or {}
                )
                resolved.append(
                    SearchHit(
                        chunk_id=str(_value(chunk, "id", default=chunk_id) or chunk_id),
                        content=str(_value(chunk, "content", default="")),
                        pages=_absolute_pages(chunk_metadata.get("page_numbers"), document_meta),
                        document_id=document_id or None,
                        document_title=_value(chunk, "document_title") or _value(document, "title"),
                        metadata={
                            **chunk_metadata,
                            "source_uri": _value(chunk, "document_uri") or _value(document, "uri"),
                            "document_meta": document_meta,
                            "logical_document_id": document_meta.get("logical_document_id"),
                            "generation_id": document_meta.get("generation_id"),
                            "raw_evidence": True,
                        },
                        search_type="lookup",
                    )
                )
        return resolved

    async def rerank(
        self, database: Path, question: str, candidates: list[SearchHit]
    ) -> list[float]:
        """Run one persistent CPU cross-encoder inside the query worker."""
        del database
        if not candidates:
            return []
        if self._persistent_reranker is None:
            from ..services.reranker_service import PersistentCrossEncoder

            self._persistent_reranker = PersistentCrossEncoder()
        from ..services.query_v2 import FusedCandidate, RetrievalCandidate

        fused = []
        for rank, hit in enumerate(candidates, 1):
            metadata = hit.metadata
            candidate = RetrievalCandidate(
                chunk_id=hit.chunk_id,
                content=hit.content,
                document_id=hit.document_id,
                logical_document_id=str(
                    metadata.get("logical_document_id") or hit.document_id or ""
                )
                or None,
                section_id=str(metadata.get("section_node_id") or "") or None,
                pages=tuple(hit.pages),
                headings=tuple(str(item) for item in metadata.get("headings", [])),
                content_hash=str(metadata.get("content_hash") or "") or None,
            )
            fused.append(
                FusedCandidate(
                    candidate=candidate,
                    fused_score=1.0 / (60 + rank),
                    ranks=(("rerank", rank),),
                    retrieval_paths=("rerank",),
                )
            )
        scored = await self._persistent_reranker.score(question, fused)
        return [score for _candidate, score in scored]

    def _basic_citation(self, cite: Any, index: int) -> Citation:
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
            primary_anchors=[],
            context_anchors=[],
            excerpt=str(_value(cite, "content", "excerpt", default="")),
            retrieval_rank=index + 1,
            rerank_score=_value(cite, "score", "rerank_score"),
            book=book,
            verification_status="provider-grounded",
        )

    async def _citation_details_with_rag(self, rag: Any, citation: Citation) -> Citation:
        if citation.primary_anchors or citation.context_anchors:
            return citation
        primary_refs: list[str] = []
        primary_chunk = None
        if citation.chunk_id:
            try:
                primary_chunk = await rag.get_chunk_by_id(citation.chunk_id)
            except Exception as exc:
                if citation.chunk_content_hash:
                    raise ConflictError(
                        "Citation evidence can no longer be verified",
                        details={"chunk_id": citation.chunk_id, "reason": "lookup_failed"},
                    ) from exc
            if primary_chunk is not None:
                primary_metadata = dict(_value(primary_chunk, "metadata", default={}) or {})
                primary_refs = _string_list(primary_metadata.get("doc_item_refs", []))
                raw_content = str(_value(primary_chunk, "content", default=""))
                actual_hash = hashlib.sha256(raw_content.encode()).hexdigest()
                start = citation.excerpt_char_start
                end = citation.excerpt_char_end
                invalid_slice = (
                    start is not None
                    and end is not None
                    and (
                        end < start
                        or end > len(raw_content)
                        or raw_content[start:end] != citation.excerpt
                    )
                )
                stable_id = primary_metadata.get("evidence_id")
                if (
                    (citation.chunk_content_hash and actual_hash != citation.chunk_content_hash)
                    or invalid_slice
                    or (citation.evidence_id and stable_id and citation.evidence_id != stable_id)
                ):
                    raise ConflictError(
                        "Citation evidence changed after the answer was generated",
                        details={"chunk_id": citation.chunk_id, "reason": "stale_evidence"},
                    )
            elif citation.chunk_content_hash:
                raise ConflictError(
                    "Citation evidence no longer exists",
                    details={"chunk_id": citation.chunk_id, "reason": "missing_chunk"},
                )
        primary_set = set(primary_refs)
        context_refs = [ref for ref in citation.doc_item_refs if ref not in primary_set]
        document = None
        if citation.document_id:
            with suppress(Exception):
                stored = await rag.get_document_by_id(citation.document_id)
                document = stored.get_docling_document() if stored is not None else None
        if document is None:
            return citation
        local_pages = getattr(document, "pages", {}) or {}
        local_first = min((int(page) for page in local_pages), default=1)
        page_offset = max(0, min(citation.pages or [1]) - local_first)
        primary = _anchors_for_refs(
            document, primary_refs or citation.doc_item_refs, page_offset=page_offset
        )
        context = _anchors_for_refs(document, context_refs, page_offset=page_offset)
        element_types = list(
            dict.fromkeys(
                anchor.element_type for anchor in [*primary, *context] if anchor.element_type
            )
        )
        return citation.model_copy(
            update={
                "primary_anchors": primary,
                "context_anchors": context,
                "element_types": element_types or citation.element_types,
            }
        )

    async def _citation(self, rag: Any, cite: Any, index: int) -> Citation:
        """Compatibility helper for clients that explicitly request rich provenance."""
        return await self._citation_details_with_rag(rag, self._basic_citation(cite, index))

    async def citation_details(self, database: Path, citation: Citation) -> Citation:
        if citation.primary_anchors or citation.context_anchors:
            return citation
        await self.ensure_database(database)
        async with self._client(database) as rag:
            return await self._citation_details_with_rag(rag, citation)

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
                [self._basic_citation(cite, index) for index, cite in enumerate(raw_citations)]
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
                [self._basic_citation(cite, index) for index, cite in enumerate(raw_citations)]
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
