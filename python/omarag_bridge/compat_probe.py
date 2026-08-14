from __future__ import annotations

import inspect
import json
from importlib.metadata import PackageNotFoundError, version

SUPPORTED_HAIKU = "0.74.0"
SUPPORTED_DOCLING = "2.119.0"


def _haiku_version() -> str:
    for distribution in ("haiku-rag", "haiku-rag-slim"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    raise RuntimeError("No Haiku RAG distribution is installed")


def _require_version(distribution: str, expected: str) -> str:
    try:
        installed = version(distribution)
    except PackageNotFoundError as exc:
        raise RuntimeError(f"Required distribution {distribution} is not installed") from exc
    if installed != expected:
        raise RuntimeError(
            f"{distribution} {installed} is outside the verified Book-v2 pin {expected}"
        )
    return installed


def _parameters(owner: type[object], method: str, required: set[str]) -> None:
    available = set(inspect.signature(getattr(owner, method)).parameters)
    missing = required - available
    if missing:
        raise RuntimeError(f"HaikuRAG.{method} is missing public parameters: {sorted(missing)}")


def main() -> None:
    from docling.chunking import HybridChunker
    from docling.datamodel.pipeline_options import (
        HeadingHierarchyOptions,
        PdfPipelineOptions,
    )
    from docling.document_converter import DocumentConverter
    from haiku.rag.client import HaikuRAG
    from haiku.rag.config import AppConfig
    from haiku.rag.embeddings import embed_chunks
    from haiku.rag.store.models.chunk import Chunk

    haiku_version = _haiku_version()
    if haiku_version != SUPPORTED_HAIKU:
        raise RuntimeError(
            f"Haiku RAG {haiku_version} is outside the verified Book-v2 pin {SUPPORTED_HAIKU}"
        )
    docling_version = _require_version("docling", SUPPORTED_DOCLING)

    _parameters(HaikuRAG, "search", {"query", "limit", "search_type", "filter"})
    _parameters(HaikuRAG, "ask", {"question", "filter", "images"})
    _parameters(HaikuRAG, "analyze", {"question", "filter", "images"})
    _parameters(HaikuRAG, "create_document_from_source", {"source", "title", "metadata"})
    _parameters(HaikuRAG, "import_document", {"docling_document", "chunks", "metadata"})
    _parameters(HaikuRAG, "update_document", {"document_id", "metadata", "title"})
    _parameters(HaikuRAG, "get_document_by_id", {"document_id"})
    _parameters(HaikuRAG, "get_chunk_by_id", {"chunk_id"})
    _parameters(HaikuRAG, "convert", {"source", "source_uri"})
    _parameters(HaikuRAG, "chunk", {"docling_document"})
    _parameters(DocumentConverter, "convert", {"source", "page_range"})
    if not isinstance(getattr(HaikuRAG, "embedder", None), property):
        raise RuntimeError("HaikuRAG.embedder is no longer a public property")
    if not callable(embed_chunks):
        raise RuntimeError("Haiku's public embed_chunks primitive is unavailable")
    if "embedding" not in Chunk.model_fields:
        raise RuntimeError("Haiku Chunk no longer accepts precomputed embeddings")

    config = AppConfig()
    for path in (
        ("processing", "conversion_options"),
        ("processing", "chunk_size"),
        ("processing", "split_pages"),
        ("reranking", "model"),
        ("search", "limit"),
        ("search", "max_context_chars"),
        ("qa", "model"),
        ("prompts", "domain_preamble"),
    ):
        current = config
        for field in path:
            if not hasattr(current, field):
                raise RuntimeError(f"Haiku AppConfig no longer exposes {'.'.join(path)}")
            current = getattr(current, field)

    if not callable(HybridChunker):
        raise RuntimeError("Docling HybridChunker is unavailable")
    hierarchy = HeadingHierarchyOptions(
        enabled=True,
        use_bookmarks=True,
        use_numbering=True,
        use_style=True,
    )
    pipeline_options = PdfPipelineOptions(
        generate_parsed_pages=True,
        heading_hierarchy_options=hierarchy,
    )
    if not pipeline_options.heading_hierarchy_options.enabled:
        raise RuntimeError("Docling heading hierarchy could not be enabled")
    print(
        json.dumps(
            {
                "compatible": True,
                "haiku_rag": haiku_version,
                "docling": docling_version,
                "boundary": "book-v2-public-api",
                "absolute_page_ranges": True,
                "heading_hierarchy": True,
            }
        )
    )


if __name__ == "__main__":
    main()
