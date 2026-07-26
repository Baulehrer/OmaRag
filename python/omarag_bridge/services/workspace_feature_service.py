from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..adapters.base import HaikuAdapter
from ..models.api import CreateSourceRequest
from ..models.domain import (
    BackupSummary,
    ConfigDocument,
    DocumentSummary,
    JobStatus,
    QualityReport,
    SourceDefinition,
)
from ..models.errors import ConflictError, EtagConflictError, NotFoundError, ReadOnlyError
from ..store import StateStore
from .workspace_service import WorkspaceService


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WorkspaceFeatureService:
    """Portable workspace features which do not duplicate Haiku's RAG store."""

    def __init__(
        self, store: StateStore, workspaces: WorkspaceService, adapter: HaikuAdapter
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.adapter = adapter
        self._backup_locks: dict[str, threading.RLock] = {}
        self._backup_locks_guard = threading.Lock()

    def _backup_lock(self, workspace_id: str) -> threading.RLock:
        with self._backup_locks_guard:
            return self._backup_locks.setdefault(workspace_id, threading.RLock())

    def documents(self, workspace_id: str) -> list[DocumentSummary]:
        return self._documents(workspace_id, include_hidden=False)

    def _documents(self, workspace_id: str, *, include_hidden: bool) -> list[DocumentSummary]:
        self.workspaces.get(workspace_id)
        hidden = self._hidden_documents(workspace_id)
        restored = self._restored_documents(workspace_id)
        documents: list[DocumentSummary] = []
        for job in self.store.list_jobs(workspace_id):
            if job.kind != "ingest" or job.status != JobStatus.COMPLETED:
                continue
            sources = job.payload.get("sources", [])
            results = (job.result or {}).get("documents", [])
            for index, source in enumerate(sources):
                location = str(source.get("path", ""))
                result = results[index] if index < len(results) else {}
                document_id = str(
                    result.get("document_id") or hashlib.sha256(location.encode()).hexdigest()[:20]
                )
                current_result = restored.get(document_id, result)
                documents.append(
                    DocumentSummary(
                        id=document_id,
                        title=Path(location).name or location,
                        source=location,
                        segment_document_ids=[
                            str(item) for item in current_result.get("segment_document_ids", [])
                        ],
                        page_count=current_result.get("page_count"),
                        parser_id="docling",
                        imported_at=job.updated_at,
                    )
                )
        unique: dict[str, DocumentSummary] = {}
        for document in documents:
            if include_hidden or document.id not in hidden:
                # Jobs are newest-first; keep the latest completed generation.
                unique.setdefault(document.id, document)
        return sorted(unique.values(), key=lambda item: item.imported_at, reverse=True)

    def _hidden_path(self, workspace_id: str) -> Path:
        return Path(self.workspaces.get(workspace_id).path) / ".oracle-hidden-documents.json"

    def _restored_path(self, workspace_id: str) -> Path:
        return Path(self.workspaces.get(workspace_id).path) / ".oracle-restored-documents.json"

    def _restored_documents(self, workspace_id: str) -> dict[str, dict[str, object]]:
        path = self._restored_path(workspace_id)
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, OSError, TypeError):
            return {}

    def _write_restored_documents(
        self, workspace_id: str, restored: dict[str, dict[str, object]]
    ) -> None:
        path = self._restored_path(workspace_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(restored, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _hidden_documents(self, workspace_id: str) -> set[str]:
        path = self._hidden_path(workspace_id)
        if not path.exists():
            return set()
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return set()

    def _write_hidden_documents(self, workspace_id: str, hidden: set[str]) -> None:
        path = self._hidden_path(workspace_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sorted(hidden), indent=2), encoding="utf-8")
        temporary.replace(path)

    async def delete_document(self, workspace_id: str, document_id: str) -> None:
        manifest = self.workspaces.get(workspace_id)
        if manifest.read_only:
            raise ReadOnlyError("Read-only workspace cannot remove documents")
        documents = {item.id: item for item in self.documents(workspace_id)}
        if document_id not in documents:
            raise NotFoundError(f"Document {document_id} was not found")
        hidden = self._hidden_documents(workspace_id)
        hidden.add(document_id)
        self._write_hidden_documents(workspace_id, hidden)
        try:
            targets = documents[document_id].segment_document_ids or [document_id]
            for target in targets:
                await self.adapter.delete_document(
                    self.workspaces.database_path(workspace_id), target
                )
        except Exception:
            # A partially removed generation must remain hidden. Restore will
            # re-index the original file before exposing it again.
            raise

    async def restore_document(self, workspace_id: str, document_id: str) -> None:
        hidden = self._hidden_documents(workspace_id)
        if document_id not in hidden:
            raise NotFoundError(f"Document {document_id} is not awaiting restore")
        all_documents = {
            item.id: item for item in self._documents(workspace_id, include_hidden=True)
        }
        document = all_documents.get(document_id)
        if document is None or not Path(document.source).is_file():
            raise ConflictError("Original PDF is unavailable; restore cannot continue")
        result = await self.adapter.ingest(
            self.workspaces.database_path(workspace_id),
            document.source,
            parser_id=document.parser_id,
            processing_profile=self.workspaces.get(workspace_id).processing_profile,
        )
        restored = self._restored_documents(workspace_id)
        restored[document_id] = result
        self._write_restored_documents(workspace_id, restored)
        hidden.discard(document_id)
        self._write_hidden_documents(workspace_id, hidden)

    def _sources_path(self, workspace_id: str) -> Path:
        return Path(self.workspaces.get(workspace_id).path) / "sources.yaml"

    def list_sources(self, workspace_id: str) -> list[SourceDefinition]:
        path = self._sources_path(workspace_id)
        if not path.exists():
            return []
        raw = path.read_text(encoding="utf-8").strip()
        if not raw or raw == "sources: []":
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConflictError(
                "sources.yaml enthaelt keine von OmaRag lesbare Definition"
            ) from exc
        return [SourceDefinition.model_validate(item) for item in data.get("sources", [])]

    def add_source(self, workspace_id: str, request: CreateSourceRequest) -> SourceDefinition:
        manifest = self.workspaces.get(workspace_id)
        if manifest.read_only:
            raise ReadOnlyError("Read-only-Workspace kann keine Quellen aendern")
        sources = self.list_sources(workspace_id)
        source = SourceDefinition(
            id=f"src-{uuid4().hex[:12]}",
            name=request.name.strip(),
            type=request.type,
            location=request.location.strip(),
            enabled=request.enabled,
        )
        sources.append(source)
        self._write_sources(workspace_id, sources)
        return source

    def delete_source(self, workspace_id: str, source_id: str) -> None:
        sources = self.list_sources(workspace_id)
        kept = [source for source in sources if source.id != source_id]
        if len(kept) == len(sources):
            raise NotFoundError(f"Quelle {source_id} wurde nicht gefunden")
        self._write_sources(workspace_id, kept)

    def get_source(self, workspace_id: str, source_id: str) -> SourceDefinition:
        for source in self.list_sources(workspace_id):
            if source.id == source_id:
                return source
        raise NotFoundError(f"Quelle {source_id} wurde nicht gefunden")

    def _write_sources(self, workspace_id: str, sources: list[SourceDefinition]) -> None:
        path = self._sources_path(workspace_id)
        temporary = path.with_suffix(".yaml.tmp")
        temporary.write_text(
            json.dumps(
                {"sources": [item.model_dump(mode="json") for item in sources]},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def quality(self, workspace_id: str) -> QualityReport:
        documents = self.documents(workspace_id)
        jobs = self.store.list_jobs(workspace_id)
        failed = sum(job.status == JobStatus.FAILED for job in jobs)
        completed = sum(job.kind == "ingest" and job.status == JobStatus.COMPLETED for job in jobs)
        issues: list[str] = []
        if not documents:
            issues.append("Der Workspace enthaelt noch keine indexierten Dokumente.")
        if failed:
            issues.append(f"{failed} Auftrag/Auftraege sind fehlgeschlagen.")
        return QualityReport(
            workspace_id=workspace_id,
            status="ok" if not issues else "warning",
            document_count=len(documents),
            completed_imports=completed,
            failed_jobs=failed,
            issues=issues,
        )

    def config(self, workspace_id: str) -> ConfigDocument:
        path = Path(self.workspaces.get(workspace_id).path) / "haiku.rag.yaml"
        content = path.read_text(encoding="utf-8")
        return ConfigDocument(content=content, etag=hashlib.sha256(content.encode()).hexdigest())

    def update_config(
        self, workspace_id: str, content: str, if_match: str | None
    ) -> ConfigDocument:
        manifest = self.workspaces.get(workspace_id)
        if manifest.read_only:
            raise ReadOnlyError("Read-only-Workspace kann nicht konfiguriert werden")
        current = self.config(workspace_id)
        if if_match is not None and if_match.strip('"') != current.etag:
            raise EtagConflictError("Konfiguration wurde zwischenzeitlich geaendert")
        self.adapter.validate_config(content)
        path = Path(manifest.path) / "haiku.rag.yaml"
        backup = path.with_suffix(".yaml.bak")
        temporary = path.with_suffix(".yaml.tmp")
        shutil.copy2(path, backup)
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        return self.config(workspace_id)

    def create_backup(self, workspace_id: str) -> BackupSummary:
        with self._backup_lock(workspace_id):
            manifest = self.workspaces.get(workspace_id)
            workspace = Path(manifest.path)
            backup_id = f"backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"
            archive = workspace / "snapshots" / f"{backup_id}.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for item in workspace.rglob("*"):
                    relative = item.relative_to(workspace)
                    if relative.parts[0] in {"snapshots", "backup-manifests"}:
                        continue
                    if ".omarag/locks" in relative.as_posix():
                        continue
                    bundle.add(item, arcname=relative, recursive=False)
            digest = _sha256_file(archive)
            summary = BackupSummary(
                id=backup_id,
                workspace_id=workspace_id,
                created_at=datetime.now(UTC),
                path=str(archive),
                size_bytes=archive.stat().st_size,
                sha256=digest,
                verified=True,
            )
            target = workspace / "backup-manifests" / f"{backup_id}.json"
            target.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
            return summary

    def list_backups(self, workspace_id: str) -> list[BackupSummary]:
        workspace = Path(self.workspaces.get(workspace_id).path)
        items = [
            BackupSummary.model_validate_json(path.read_text(encoding="utf-8"))
            for path in (workspace / "backup-manifests").glob("*.json")
        ]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def verify_backup(self, workspace_id: str, backup_id: str) -> BackupSummary:
        for backup in self.list_backups(workspace_id):
            if backup.id == backup_id:
                archive = Path(backup.path)
                verified = archive.is_file() and _sha256_file(archive) == backup.sha256
                return backup.model_copy(update={"verified": verified})
        raise NotFoundError(f"Sicherung {backup_id} wurde nicht gefunden")

    def restore_backup(
        self, workspace_id: str, backup_id: str
    ) -> tuple[BackupSummary, BackupSummary]:
        with self._backup_lock(workspace_id):
            return self._restore_backup_locked(workspace_id, backup_id)

    def _restore_backup_locked(
        self, workspace_id: str, backup_id: str
    ) -> tuple[BackupSummary, BackupSummary]:
        manifest = self.workspaces.get(workspace_id)
        if manifest.read_only:
            raise ReadOnlyError("Read-only-Workspace kann nicht wiederhergestellt werden")
        selected = self.verify_backup(workspace_id, backup_id)
        if not selected.verified:
            raise ConflictError("Sicherung ist beschaedigt oder ihre Pruefsumme stimmt nicht")

        workspace = Path(manifest.path)
        archive = Path(selected.path)
        staging = Path(tempfile.mkdtemp(prefix=f".{workspace.name}.restore-", dir=workspace.parent))
        retired = workspace.parent / f".{workspace.name}.previous-{uuid4().hex[:8]}"
        switched = False
        try:
            with tarfile.open(archive, "r:gz") as bundle:
                members = bundle.getmembers()
                self._validate_archive_members(members)
                bundle.extractall(staging, members=members, filter="data")
            if (
                not (staging / "workspace.toml").is_file()
                or not (staging / "haiku.rag.yaml").is_file()
            ):
                raise ConflictError("Sicherung enthaelt keinen vollstaendigen Workspace")

            safety = self.create_backup(workspace_id)
            for persistent in ("snapshots", "backup-manifests"):
                source = workspace / persistent
                if source.exists():
                    shutil.copytree(source, staging / persistent, dirs_exist_ok=True)

            workspace.rename(retired)
            staging.rename(workspace)
            switched = True
            shutil.rmtree(retired)
            return selected, safety
        except Exception:
            if retired.exists() and not workspace.exists():
                retired.rename(workspace)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if switched and retired.exists():
                shutil.rmtree(retired)

    @staticmethod
    def _validate_archive_members(members: list[tarfile.TarInfo]) -> None:
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ConflictError("Sicherung enthaelt einen unsicheren Pfad")
            if member.issym() or member.islnk() or member.isdev():
                raise ConflictError("Sicherung enthaelt einen nicht erlaubten Dateityp")
