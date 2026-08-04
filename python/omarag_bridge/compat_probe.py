from __future__ import annotations

import inspect
import json
from importlib.metadata import PackageNotFoundError, version


def _haiku_version() -> str:
    for distribution in ("haiku-rag", "haiku-rag-slim"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    raise RuntimeError("No Haiku RAG distribution is installed")


def _parameters(owner: type[object], method: str, required: set[str]) -> None:
    available = set(inspect.signature(getattr(owner, method)).parameters)
    missing = required - available
    if missing:
        raise RuntimeError(f"HaikuRAG.{method} is missing public parameters: {sorted(missing)}")


def main() -> None:
    from docling.chunking import HybridChunker
    from haiku.rag.client import HaikuRAG
    from haiku.rag.config import AppConfig

    _parameters(HaikuRAG, "search", {"query", "limit", "search_type", "filter"})
    _parameters(HaikuRAG, "ask", {"question", "filter", "images"})
    _parameters(HaikuRAG, "analyze", {"question", "filter", "images"})
    _parameters(HaikuRAG, "create_document_from_source", {"source", "title", "metadata"})
    _parameters(HaikuRAG, "import_document", {"docling_document", "chunks", "metadata"})
    _parameters(HaikuRAG, "update_document", {"document_id", "metadata", "title"})
    _parameters(HaikuRAG, "get_document_by_id", {"document_id"})
    _parameters(HaikuRAG, "get_chunk_by_id", {"chunk_id"})

    config = AppConfig()
    for path in (
        ("processing", "conversion_options"),
        ("processing", "chunk_size"),
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
    print(
        json.dumps(
            {
                "compatible": True,
                "haiku_rag": _haiku_version(),
                "boundary": "documented-public-api",
            }
        )
    )


if __name__ == "__main__":
    main()
