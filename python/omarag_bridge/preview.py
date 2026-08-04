from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
from urllib.parse import unquote, urlparse

from .models.domain import Citation
from .models.errors import ConflictError, NotFoundError


def _source_path(source_uri: str | None) -> Path:
    if not source_uri:
        raise NotFoundError("Citation has no original source")
    parsed = urlparse(source_uri)
    if parsed.scheme != "file":
        raise ConflictError("Preview currently supports local PDF evidence only")
    path = Path(unquote(parsed.path)).resolve()
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise NotFoundError("The cited PDF is no longer available")
    return path


def _render(path: Path, citation: Citation, cache_dir: Path, max_px: int) -> bytes:
    import pypdfium2 as pdfium

    anchor = next(iter(citation.primary_anchors or citation.context_anchors), None)
    page_number = anchor.page if anchor else next(iter(citation.pages), 1)
    material = (
        f"{path}:{path.stat().st_mtime_ns}:{page_number}:{max_px}:"
        f"{anchor.model_dump_json() if anchor else 'page'}"
    )
    cache_key = hashlib.sha256(material.encode()).hexdigest()
    cache_path = cache_dir / f"{cache_key}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    document = pdfium.PdfDocument(str(path))
    try:
        if page_number < 1 or page_number > len(document):
            raise NotFoundError(f"PDF page {page_number} does not exist")
        page = document[page_number - 1]
        try:
            width, height = page.get_size()
            scale = min(3.0, max(1.0, max_px / max(width, height)))
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()

    if anchor:
        padding = 0.035
        x0 = max(0.0, anchor.x0 - padding)
        y0 = max(0.0, anchor.y0 - padding)
        x1 = min(1.0, anchor.x1 + padding)
        y1 = min(1.0, anchor.y1 + padding)
        image = image.crop(
            (
                int(x0 * image.width),
                int(y0 * image.height),
                max(int(x1 * image.width), int(x0 * image.width) + 1),
                max(int(y1 * image.height), int(y0 * image.height) + 1),
            )
        )
    if max(image.size) > max_px:
        image.thumbnail((max_px, max_px))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    payload = output.getvalue()
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    temporary.replace(cache_path)
    _prune(cache_dir)
    return payload


def _prune(cache_dir: Path, limit_bytes: int = 300 * 1024**2) -> None:
    files = sorted(cache_dir.glob("*.png"), key=lambda item: item.stat().st_mtime, reverse=True)
    total = 0
    for path in files:
        total += path.stat().st_size
        if total > limit_bytes:
            path.unlink(missing_ok=True)


async def render_citation_preview(citation: Citation, cache_dir: Path, max_px: int) -> bytes:
    return await asyncio.to_thread(
        _render, _source_path(citation.source_uri), citation, cache_dir, max_px
    )
