from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from uuid import uuid4

from ..adapters.base import HaikuAdapter
from ..models.api import SourceInput
from ..models.domain import (
    BookMetadata,
    ImportCandidate,
    ImportPreflightBatch,
    MetadataProposal,
)
from ..models.errors import ConflictError, NotFoundError, ReadOnlyError
from ..store import StateStore
from .workspace_service import WorkspaceService

_ISBN_RE = re.compile(
    r"(?i)\bISBN(?:-1[03])?\s*:?[\s-]*((?:97[89][\s-]*)?[0-9X](?:[0-9X\s-]{7,20})[0-9X])\b"
)
_EDITION_RE = re.compile(
    r"(?i)\b(\d{1,2})\.?\s*(?:überarbeitete\s+|aktualisierte\s+|neue\s+)?"
    r"(?:Auflage|Ausgabe|edition|ed\.)\b"
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_TITLE_REJECT = re.compile(r"(?i)^(isbn|copyright|©|www\.|http|inhalt|contents|seite\s+\d+|\d+)$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


_FICLONE = 0x40049409


def _stream_copy_and_hash(source: Path, target: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as incoming, target.open("r+b") as outgoing:
        outgoing.seek(0)
        outgoing.truncate()
        while block := incoming.read(4 * 1024 * 1024):
            digest.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    shutil.copystat(source, target, follow_symlinks=True)
    return digest.hexdigest()


def _reflink_and_hash(source: Path, target: Path) -> str | None:
    """Create an independent copy-on-write clone when the filesystem supports it."""
    try:
        with source.open("rb") as incoming, target.open("r+b") as outgoing:
            fcntl.ioctl(outgoing.fileno(), _FICLONE, incoming.fileno())
        shutil.copystat(source, target, follow_symlinks=True)
        return file_sha256(target)
    except OSError:
        return None


def archive_source(
    workspace_path: Path, source: Path, expected_fingerprint: str | None = None
) -> tuple[Path, str, str]:
    """Keep an immutable original, preferring a cheap copy-on-write clone.

    The fallback copies and hashes in one streaming pass.  A hardlink is
    intentionally not used: editing the user's original must never alter the
    indexed evidence.
    """
    if not source.is_file():
        raise NotFoundError(f"Source {source} was not found")
    originals = workspace_path / "sources" / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() or ".bin"
    if expected_fingerprint:
        target = originals / f"{expected_fingerprint}{suffix}"
        if target.exists():
            source_size = source.stat().st_size
            target_stat = target.stat()
            if target_stat.st_size != source_size:
                raise ConflictError("Managed source size does not match the import source")
            # Verify legacy writable archives once, then make subsequent reuse
            # a metadata-only operation. New archives are immutable below.
            if target_stat.st_mode & 0o222:
                if file_sha256(target) != expected_fingerprint:
                    raise ConflictError("Managed source hash does not match its filename")
                target.chmod(target_stat.st_mode & ~0o222)
            return target, expected_fingerprint, "existing"

    prefix = f".{(expected_fingerprint or 'archive')[:12]}-"
    fd, temporary_name = tempfile.mkstemp(prefix=prefix, dir=originals)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        fingerprint = _reflink_and_hash(source, temporary)
        storage_mode = "reflink"
        if fingerprint is None:
            fingerprint = _stream_copy_and_hash(source, temporary)
            storage_mode = "copy"
        if expected_fingerprint and fingerprint != expected_fingerprint:
            raise ConflictError("Source changed while OmaRag was archiving it")
        target = originals / f"{fingerprint}{suffix}"
        if target.exists():
            temporary.unlink(missing_ok=True)
            return target, fingerprint, "existing"
        temporary.replace(target)
        target.chmod(target.stat().st_mode & ~0o222)
    finally:
        temporary.unlink(missing_ok=True)
    return target, fingerprint, storage_mode


def _normalize_isbn(value: str) -> str | None:
    digits = re.sub(r"[^0-9X]", "", value.upper())
    if len(digits) == 10:
        total = sum(
            (10 - index) * (10 if char == "X" else int(char)) for index, char in enumerate(digits)
        )
        return digits if total % 11 == 0 else None
    if len(digits) == 13:
        total = sum(
            int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(digits[:12])
        )
        check = (10 - total % 10) % 10
        return digits if check == int(digits[-1]) else None
    return None


def _slug_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).strip()


def _work_id(title: str, authors: list[str]) -> str:
    identity = f"{_slug_text(title)}|{_slug_text(authors[0]) if authors else ''}"
    return f"work-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"


def _split_authors(value: str) -> list[str]:
    items = re.split(r"\s*(?:;|\band\b|\bund\b)\s*", value, flags=re.IGNORECASE)
    return [item.strip(" ,") for item in items if item.strip(" ,")]


def _pdf_signals(path: Path) -> tuple[dict[str, str], str]:
    if path.suffix.lower() != ".pdf":
        return {}, ""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        metadata = {
            str(key): str(value) for key, value in document.get_metadata_dict().items() if value
        }
        page_count = len(document)
        indexes = list(range(min(page_count, 12)))
        indexes.extend(range(max(12, page_count - 3), page_count))
        text_parts: list[str] = []
        for index in dict.fromkeys(indexes):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    text_parts.append(text_page.get_text_range())
                finally:
                    text_page.close()
            finally:
                page.close()
        return metadata, "\n".join(text_parts)
    finally:
        document.close()


def _language(text: str) -> tuple[str, float]:
    lowered = f" {text.casefold()} "
    german = sum(
        lowered.count(word) for word in (" der ", " die ", " und ", " auflage ", " seite ")
    )
    english = sum(
        lowered.count(word) for word in (" the ", " and ", " edition ", " chapter ", " page ")
    )
    if german == english == 0:
        return "de", 0.4
    return ("de", 0.8) if german >= english else ("en", 0.8)


def inspect_source(source: Path) -> ImportCandidate:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise NotFoundError(f"Source {source} was not found")
    before = source.stat()
    fingerprint = file_sha256(source)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ConflictError("Source changed while OmaRag was inspecting it")
    embedded, text = _pdf_signals(source)
    proposals: list[MetadataProposal] = []

    title = embedded.get("Title", "").strip()
    title_source = "pdf-metadata"
    title_confidence = 0.95
    if not title:
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        title = next(
            (line for line in lines if 5 <= len(line) <= 180 and not _TITLE_REJECT.match(line)),
            source.stem.replace("_", " ").replace("-", " ").strip(),
        )
        title_source = "title-page" if title != source.stem else "filename"
        title_confidence = 0.75 if title_source == "title-page" else 0.5
    proposals.append(
        MetadataProposal(
            field="title", value=title, source=title_source, confidence=title_confidence
        )
    )

    author_value = embedded.get("Author", "").strip()
    authors = _split_authors(author_value) if author_value else []
    if authors:
        proposals.append(
            MetadataProposal(field="authors", value=authors, source="pdf-metadata", confidence=0.9)
        )

    isbns = list(
        dict.fromkeys(
            valid
            for match in _ISBN_RE.finditer(text)
            if (valid := _normalize_isbn(match.group(1))) is not None
        )
    )
    if isbns:
        proposals.append(
            MetadataProposal(field="isbn", value=isbns, source="imprint", confidence=0.98)
        )

    edition_match = _EDITION_RE.search(text)
    edition_number = int(edition_match.group(1)) if edition_match else None
    edition_label = edition_match.group(0).strip() if edition_match else None
    if edition_match:
        proposals.append(
            MetadataProposal(
                field="edition_label", value=edition_label, source="imprint", confidence=0.9
            )
        )
        proposals.append(
            MetadataProposal(
                field="edition_number", value=edition_number, source="imprint", confidence=0.95
            )
        )

    metadata_date = embedded.get("CreationDate", "") + " " + embedded.get("ModDate", "")
    year_matches = _YEAR_RE.findall(metadata_date) or _YEAR_RE.findall(text[:25_000])
    publication_year = int(year_matches[-1]) if year_matches else None
    if publication_year:
        proposals.append(
            MetadataProposal(
                field="publication_year",
                value=publication_year,
                source="pdf-metadata" if _YEAR_RE.search(metadata_date) else "imprint",
                confidence=0.7,
            )
        )

    language, language_confidence = _language(text[:30_000])
    proposals.append(
        MetadataProposal(
            field="language",
            value=language,
            source="language-heuristic",
            confidence=language_confidence,
        )
    )
    book = BookMetadata(
        work_id=_work_id(title, authors),
        title=title,
        authors=authors,
        edition_label=edition_label,
        edition_number=edition_number,
        publication_year=publication_year,
        isbn=isbns,
        language=language,
        confirmed=False,
    )
    issues: list[str] = []
    if not authors:
        issues.append("Author could not be detected; confirm or add it before indexing.")
    if not edition_label:
        issues.append("Edition could not be detected.")
    if not isbns:
        issues.append("No checksum-valid ISBN was detected.")
    return ImportCandidate(
        id=f"candidate-{uuid4().hex[:12]}",
        source=str(source),
        fingerprint=fingerprint,
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
        metadata=book,
        proposals=proposals,
        issues=issues,
    )


class TextbookService:
    def __init__(
        self,
        store: StateStore,
        workspaces: WorkspaceService,
        adapter: HaikuAdapter,
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.adapter = adapter

    def preflight(self, workspace_id: str, sources: list[SourceInput]) -> ImportPreflightBatch:
        self.workspaces.get(workspace_id)
        candidates = [inspect_source(Path(source.path)) for source in sources]
        batch = ImportPreflightBatch(id=f"preflight-{uuid4().hex[:12]}", candidates=candidates)
        self.store.save_import_preflight(batch.id, workspace_id, batch.model_dump(mode="json"))
        return batch

    def validate_commit(
        self, workspace_id: str, preflight_id: str, sources: list[SourceInput]
    ) -> list[SourceInput]:
        payload = self.store.get_import_preflight(preflight_id, workspace_id)
        known = {item["id"]: item for item in payload.get("candidates", [])}
        validated: list[SourceInput] = []
        for source in sources:
            candidate = known.get(source.candidate_id or "")
            if candidate is None:
                raise ConflictError("Import candidate does not belong to this preflight")
            current = Path(source.path).expanduser().resolve()
            if str(current) != candidate["source"]:
                raise ConflictError("Import source changed after metadata review")
            try:
                stat = current.stat()
            except OSError as exc:
                raise ConflictError("Import source is no longer readable") from exc
            unchanged = (
                int(candidate.get("size_bytes", -1)) == stat.st_size
                and int(candidate.get("mtime_ns", -1)) == stat.st_mtime_ns
            )
            fingerprint = str(candidate["fingerprint"])
            if not unchanged or (source.fingerprint and source.fingerprint != fingerprint):
                raise ConflictError("Import source content changed after metadata review")
            if source.metadata is None or not source.metadata.confirmed:
                raise ConflictError("Bibliographic metadata must be confirmed before indexing")
            validated.append(
                source.model_copy(update={"path": str(current), "fingerprint": fingerprint})
            )
        return validated

    async def update_metadata(
        self, workspace_id: str, logical_document_id: str, metadata: BookMetadata
    ) -> None:
        if self.workspaces.get(workspace_id).read_only:
            raise ReadOnlyError("Read-only workspace cannot change book metadata")
        if not metadata.confirmed:
            raise ConflictError("Bibliographic metadata must be confirmed before saving")
        record = self.store.book_record(workspace_id, logical_document_id)
        await self.adapter.update_document_metadata(
            self.workspaces.database_path(workspace_id),
            [str(item["segment_document_id"]) for item in record["segments"]],
            metadata.model_dump(mode="json"),
        )
        self.store.update_book_metadata(
            workspace_id, logical_document_id, metadata.model_dump(mode="json")
        )
