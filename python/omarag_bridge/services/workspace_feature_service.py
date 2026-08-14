from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from uuid import uuid4

from ruamel.yaml import YAML

from ..adapters.base import HaikuAdapter
from ..models.api import CreateSourceRequest, ModelDefaultsRequest
from ..models.domain import (
    BackupSummary,
    BookMetadata,
    ConfigDocument,
    DocumentPurgePlan,
    DocumentPurgeResult,
    DocumentQuality,
    DocumentSummary,
    JobStatus,
    ModelDefaultsPreflight,
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


def _secure_write_text(path: Path, content: str) -> None:
    """Atomically publish managed text with private directory/file modes."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            stream.write(content)
            temporary = Path(stream.name)
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class WorkspaceFeatureService:
    """Portable workspace features which do not duplicate Haiku's RAG store."""

    def __init__(
        self, store: StateStore, workspaces: WorkspaceService, adapter: HaikuAdapter
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.adapter = adapter
        self.content_egress_guard: Callable[[str, str], None] | None = None
        self._backup_locks: dict[str, threading.RLock] = {}
        self._backup_locks_guard = threading.Lock()

    def _backup_lock(self, workspace_id: str) -> threading.RLock:
        with self._backup_locks_guard:
            return self._backup_locks.setdefault(workspace_id, threading.RLock())

    def _validate_ollama_endpoint(self, content: str) -> None:
        data = self._round_trip_yaml().load(content) or {}
        providers = data.get("providers") or {}
        ollama = providers.get("ollama") if isinstance(providers, dict) else None
        configured = ollama.get("base_url") if isinstance(ollama, dict) else None
        expected = self.workspaces.ollama_url.rstrip("/")
        if not configured or str(configured).rstrip("/") != expected:
            raise ConflictError(
                "Workspace providers.ollama.base_url must match the configured local runtime",
                details={"expected_base_url": expected},
            )

    def documents(self, workspace_id: str) -> list[DocumentSummary]:
        return self._documents(workspace_id, include_hidden=False)

    def _documents(self, workspace_id: str, *, include_hidden: bool) -> list[DocumentSummary]:
        self.workspaces.get(workspace_id)
        hidden = self._hidden_documents(workspace_id)
        restored = self._restored_documents(workspace_id)
        records = {
            item["logical_document_id"]: item for item in self.store.book_records(workspace_id)
        }
        documents: list[DocumentSummary] = []
        for job in self.store.list_jobs(workspace_id):
            if job.kind not in {"ingest", "reindex"} or job.status != JobStatus.COMPLETED:
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
                record = records.get(document_id, {})
                book_payload = record.get("metadata") or current_result.get("book_metadata")
                quality_payload = record.get("quality") or current_result.get("quality")
                documents.append(
                    DocumentSummary(
                        id=document_id,
                        title=(book_payload or {}).get("title") or Path(location).name or location,
                        source=location,
                        segment_document_ids=[
                            str(item) for item in current_result.get("segment_document_ids", [])
                        ],
                        page_count=current_result.get("page_count"),
                        parser_id="docling",
                        imported_at=job.updated_at,
                        fingerprint=current_result.get("fingerprint"),
                        generation_id=current_result.get("generation_id"),
                        cache_status=current_result.get("cache_status"),
                        pipeline_stats=current_result.get("pipeline_stats", {}),
                        managed_source=record.get("managed_source")
                        or current_result.get("managed_source"),
                        book=(BookMetadata.model_validate(book_payload) if book_payload else None),
                        quality=(
                            DocumentQuality.model_validate(quality_payload)
                            if quality_payload
                            else None
                        ),
                        pipeline_version=str(
                            record.get("pipeline_version")
                            or current_result.get("pipeline_version", "textbook-v1")
                        ),
                        structure_mode=str(
                            (quality_payload or {}).get("structure_mode", "unknown")
                        ),
                        structure_confidence=float(
                            (quality_payload or {}).get("structure_confidence", 0.0)
                        ),
                        toc_found=bool((quality_payload or {}).get("toc_found", False)),
                        index_found=bool((quality_payload or {}).get("index_found", False)),
                        glossary_found=bool((quality_payload or {}).get("glossary_found", False)),
                        fallback_used=bool((quality_payload or {}).get("fallback_used", False)),
                        size_bytes=int(current_result.get("size_bytes") or 0),
                        archive_mode=str(current_result.get("archive_mode") or "unknown"),
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
        _secure_write_text(path, json.dumps(restored, indent=2))

    def _hidden_documents(self, workspace_id: str) -> set[str]:
        path = self._hidden_path(workspace_id)
        persisted = self.store.hidden_document_ids(workspace_id)
        if not path.exists():
            return persisted
        try:
            return persisted | set(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return persisted

    def _write_hidden_documents(self, workspace_id: str, hidden: set[str]) -> None:
        # The SQLite set is the operational query filter. Publish it first so
        # an adapter deletion failure cannot leave surviving segments visible.
        self.store.replace_hidden_documents(workspace_id, hidden)
        path = self._hidden_path(workspace_id)
        _secure_write_text(path, json.dumps(sorted(hidden), indent=2))

    def reconcile_hidden_documents(self, workspace_id: str) -> None:
        """Migrate/repair legacy sidecar deletions into the query-time store."""

        hidden = self._hidden_documents(workspace_id)
        if hidden != self.store.hidden_document_ids(workspace_id):
            self.store.replace_hidden_documents(workspace_id, hidden)

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
        self.store.clear_answer_cache(workspace_id)
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
        restore_source = document.managed_source if document else None
        if document is None or not restore_source or not Path(restore_source).is_file():
            raise ConflictError("Original PDF is unavailable; restore cannot continue")
        if document.pipeline_version in {"book-index-v2", "book-index-v3"}:
            generation_id = document.generation_id or ""
            if generation_id and self.store.hidden_document_rebuilt(
                workspace_id,
                document_id,
                generation_id,
            ):
                # A confirmed full rebuild has already recreated the exact
                # document under the current generation/config. Publishing is
                # therefore a logical flag change, not an unsafe ad-hoc ingest.
                hidden.discard(document_id)
                self._write_hidden_documents(workspace_id, hidden)
                return
            raise ConflictError(
                "A structured book can only be restored through a confirmed full reindex; "
                "the hidden document remains excluded from every query"
            )
        if self.content_egress_guard is not None:
            self.content_egress_guard(workspace_id, self.workspaces.ollama_url)
        result = await self.adapter.ingest(
            self.workspaces.database_path(workspace_id),
            restore_source,
            parser_id=document.parser_id,
            processing_profile=self.workspaces.get(workspace_id).processing_profile,
            metadata=document.book,
            original_source=document.source,
        )
        fingerprint = document.fingerprint or _sha256_file(Path(restore_source))
        self.store.upsert_document(workspace_id, document.source, fingerprint, result)
        restored = self._restored_documents(workspace_id)
        restored[document_id] = result
        self._write_restored_documents(workspace_id, restored)
        hidden.discard(document_id)
        self._write_hidden_documents(workspace_id, hidden)

    def document_purge_preflight(self, workspace_id: str, document_id: str) -> DocumentPurgePlan:
        manifest = self.workspaces.get(workspace_id)
        if manifest.read_only:
            raise ReadOnlyError("Read-only workspace cannot purge documents")
        state = self.store.document_purge_state(workspace_id, document_id)
        backups = self.list_backups(workspace_id)
        backup_ids = [item.id for item in backups]
        pinned_backup_ids = [item.id for item in backups if item.pinned]
        created = datetime.now(UTC)
        material = json.dumps(
            {
                "workspace_id": workspace_id,
                "document_id": document_id,
                "generation_id": state["generation_id"],
                "fingerprint": state["fingerprint"],
                "backups": backup_ids,
                "pinned_runs": state["pinned_run_ids"],
                "nonce": uuid4().hex,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        plan_id = "sha256:" + hashlib.sha256(material.encode()).hexdigest()[:24]
        return DocumentPurgePlan(
            plan_id=plan_id,
            workspace_id=workspace_id,
            document_id=document_id,
            generation_id=str(state["generation_id"]),
            fingerprint=str(state["fingerprint"]),
            segment_document_ids=list(state["segment_document_ids"]),
            media_assets=int(state["media_assets"]),
            pinned_run_ids=list(state["pinned_run_ids"]),
            backup_ids=backup_ids,
            pinned_backup_ids=pinned_backup_ids,
            requires_backup_confirmation=bool(backup_ids),
            can_purge=not state["pinned_run_ids"] and not pinned_backup_ids,
            created_at=created,
            expires_at=created + timedelta(minutes=15),
        )

    async def purge_document(
        self,
        plan: DocumentPurgePlan,
        *,
        backup_confirmed: bool,
    ) -> DocumentPurgeResult:
        """Irreversibly remove one exactly pinned book and its derived state."""

        if plan.expires_at <= datetime.now(UTC):
            raise ConflictError("Document purge preflight expired")
        if not plan.can_purge or plan.pinned_run_ids or plan.pinned_backup_ids:
            raise ConflictError(
                "Pinned runs or backups block permanent document deletion",
                details={
                    "run_ids": plan.pinned_run_ids,
                    "backup_ids": plan.pinned_backup_ids,
                },
            )
        current_backups = self.list_backups(plan.workspace_id)
        current_backup_ids = [item.id for item in current_backups]
        if current_backup_ids != plan.backup_ids:
            raise ConflictError("Workspace backups changed after purge preflight")
        if any(item.pinned for item in current_backups):
            raise ConflictError("A pinned backup blocks permanent document deletion")
        if current_backups and not backup_confirmed:
            raise ConflictError("PURGE_BACKUPS confirmation is required")

        state = self.store.document_purge_state(plan.workspace_id, plan.document_id)
        if (
            state["generation_id"] != plan.generation_id
            or state["fingerprint"] != plan.fingerprint
            or list(state["segment_document_ids"]) != plan.segment_document_ids
        ):
            raise ConflictError("Document changed after purge preflight")
        if state["pinned_run_ids"]:
            raise ConflictError("A pinned run now references this document")

        hidden = self._hidden_documents(plan.workspace_id)
        hidden.add(plan.document_id)
        self._write_hidden_documents(plan.workspace_id, hidden)
        targets = plan.segment_document_ids or [plan.document_id]
        for target in targets:
            await self.adapter.delete_document(
                self.workspaces.database_path(plan.workspace_id), target
            )

        removed = self.store.purge_document_state(
            plan.workspace_id,
            plan.document_id,
            expected_generation_id=plan.generation_id,
            expected_fingerprint=plan.fingerprint,
        )
        restored = self._restored_documents(plan.workspace_id)
        if restored.pop(plan.document_id, None) is not None:
            restored_path = self._restored_path(plan.workspace_id)
            if restored:
                self._write_restored_documents(plan.workspace_id, restored)
            else:
                restored_path.unlink(missing_ok=True)
        workspace = Path(self.workspaces.get(plan.workspace_id).path).resolve()
        managed_source = Path(str(removed.get("managed_source") or ""))
        original_removed = False
        if managed_source.is_file():
            resolved_source = managed_source.resolve()
            originals = (workspace / "sources" / "originals").resolve()
            if resolved_source.is_relative_to(originals) and not any(
                Path(str(record.get("managed_source") or "")).resolve() == resolved_source
                for record in self.store.book_records(plan.workspace_id)
            ):
                resolved_source.unlink(missing_ok=True)
                original_removed = True

        removed_backups = 0
        if current_backups:
            with self._backup_lock(plan.workspace_id):
                for backup in current_backups:
                    archive = Path(backup.path)
                    if archive.parent.resolve() == (workspace / "snapshots").resolve():
                        archive.unlink(missing_ok=True)
                    (workspace / "backup-manifests" / f"{backup.id}.json").unlink(missing_ok=True)
                    removed_backups += 1

        shutil.rmtree(workspace / ".oracle-cache" / "previews", ignore_errors=True)
        hidden = self._hidden_documents(plan.workspace_id)
        hidden.discard(plan.document_id)
        self._write_hidden_documents(plan.workspace_id, hidden)

        from ..models.media import MediaAsset
        from .media_service import mark_media_blob_references, sweep_unreferenced_media_blobs

        assets = [
            MediaAsset.model_validate(item)
            for item in self.store.all_book_media_assets(plan.workspace_id)
        ]
        marked = mark_media_blob_references(
            assets,
            visual_evidence=self.store.run_visual_evidence(plan.workspace_id),
        )
        sweep_unreferenced_media_blobs(
            self.workspaces.database_path(plan.workspace_id), marked, dry_run=False
        )
        return DocumentPurgeResult(
            workspace_id=plan.workspace_id,
            document_id=plan.document_id,
            generation_id=plan.generation_id,
            removed_segments=len(targets),
            removed_media_assets=int(removed["media_assets"]),
            removed_backups=removed_backups,
            original_removed=original_removed,
        )

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
        _secure_write_text(
            path,
            json.dumps(
                {"sources": [item.model_dump(mode="json") for item in sources]},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )

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
        for document in documents:
            if document.quality is not None:
                issues.extend(f"{document.title}: {issue}" for issue in document.quality.issues)
        latest_evaluation = self.store.latest_evaluation(workspace_id)
        variants = (latest_evaluation or {}).get("variants", {})
        retrieval_metrics = {
            f"{variant}.{metric}": float(value)
            for variant, metrics in variants.items()
            for metric, value in metrics.items()
        }
        return QualityReport(
            workspace_id=workspace_id,
            status="ok" if not issues else "warning",
            document_count=len(documents),
            completed_imports=completed,
            failed_jobs=failed,
            issues=issues,
            latest_evaluation_id=(latest_evaluation or {}).get("id"),
            retrieval_metrics=retrieval_metrics,
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
        self._validate_ollama_endpoint(content)
        self.adapter.validate_config(content)
        if self.store.book_records(workspace_id):
            yaml = self._round_trip_yaml()
            before = yaml.load(current.content) or {}
            after = yaml.load(content) or {}

            def embedding_identity(data: dict[str, object]) -> tuple[str, str, int]:
                model = (data.get("embeddings") or {}).get("model") or {}  # type: ignore[union-attr]
                return (
                    str(model.get("provider") or ""),
                    str(model.get("name") or ""),
                    int(model.get("vector_dim") or 0),
                )

            if embedding_identity(before) != embedding_identity(after):
                raise ConflictError(
                    "Embedding provider, model, and dimension are pinned to the current index; "
                    "use the full rebuild workflow"
                )
        path = Path(manifest.path) / "haiku.rag.yaml"
        backup = path.with_suffix(".yaml.bak")
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        _secure_write_text(path, content)
        self.store.clear_answer_cache(workspace_id)
        return self.config(workspace_id)

    def render_model_defaults(
        self,
        workspace_id: str,
        request: ModelDefaultsRequest,
        if_match: str | None,
        *,
        profile_metadata: dict[str, object] | None = None,
    ) -> ConfigDocument:
        """Render and validate a model profile without publishing it.

        Embedding-changing profiles use this method to produce the immutable
        staged configuration consumed by the reindex job.  Keeping rendering
        separate from activation makes the old index and its matching config
        usable while downloads and admission checks are still in progress.
        """

        manifest = self.workspaces.get(workspace_id)
        if manifest.read_only:
            raise ReadOnlyError("Read-only workspace cannot change model defaults")
        current = self.config(workspace_id)
        if if_match is not None and if_match.strip('"') != current.etag:
            raise EtagConflictError("Konfiguration wurde zwischenzeitlich geaendert")
        yaml = self._round_trip_yaml()
        data = yaml.load(current.content) or {}
        embeddings = data.setdefault("embeddings", {})
        embedding_model = embeddings.setdefault("model", {})
        embedding_model["provider"] = request.embedding_provider
        embedding_model["name"] = request.embedding
        embedding_model["vector_dim"] = request.vector_dim
        reranking = data.setdefault("reranking", {})
        rerank_model = reranking.setdefault("model", {})
        rerank_model["provider"] = request.rerank_provider
        rerank_model["name"] = request.rerank
        qa = data.setdefault("qa", {})
        qa_model = qa.setdefault("model", {})
        qa_model["provider"] = "ollama"
        qa_model["name"] = request.chat
        qa_model["vision"] = bool(request.vl)
        oracle = data.setdefault("oracle", {})
        defaults = oracle.setdefault("model_defaults", {})
        defaults["chat"] = request.chat
        # Structure extraction is an explicit role, even when it shares the
        # selected chat artifact.
        defaults["structure"] = request.chat
        defaults["vl"] = request.vl
        defaults["embedding"] = request.embedding
        defaults["rerank"] = request.rerank
        if profile_metadata is not None:
            oracle["model_profile"] = profile_metadata
        output = StringIO()
        yaml.dump(data, output)
        content = output.getvalue()
        self._validate_ollama_endpoint(content)
        self.adapter.validate_config(content)
        return ConfigDocument(
            content=content,
            etag=hashlib.sha256(content.encode()).hexdigest(),
        )

    def activate_model_defaults_for_reindex(
        self,
        workspace_id: str,
        content: str,
        expected_current_etag: str,
        expected_target_etag: str,
        generation_id: str,
    ) -> ConfigDocument:
        """Publish a staged config only behind an active maintenance gate.

        This deliberately bypasses the normal embedding-identity guard, but is
        fail-closed: only the latest maintenance generation may activate it.
        Repeating the call after a crash is idempotent when the target config
        was already atomically replaced.
        """

        manifest = self.workspaces.get(workspace_id)
        if manifest.read_only:
            raise ReadOnlyError("Read-only workspace cannot activate a model profile")
        generation = self.store.index_generation(workspace_id, generation_id)
        latest = self.store.workspace_index_generation(workspace_id)
        if (
            generation is None
            or latest is None
            or latest["generation_id"] != generation_id
            or generation["status"] not in {"maintenance", "maintenance_failed"}
        ):
            raise ConflictError(
                "A staged embedding config can only be activated by the current rebuild"
            )
        actual_target_etag = hashlib.sha256(content.encode()).hexdigest()
        if actual_target_etag != expected_target_etag:
            raise ConflictError("Staged model configuration failed its integrity check")
        self._validate_ollama_endpoint(content)
        current = self.config(workspace_id)
        if current.etag == expected_target_etag:
            return current
        if current.etag != expected_current_etag:
            raise EtagConflictError("Workspace configuration changed before the rebuild boundary")
        self.adapter.validate_config(content)
        path = Path(manifest.path) / "haiku.rag.yaml"
        backup = path.with_suffix(".yaml.bak")
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        _secure_write_text(path, content)
        self.store.clear_answer_cache(workspace_id)
        activated = self.config(workspace_id)
        if activated.etag != expected_target_etag:
            raise ConflictError("Activated model configuration failed its integrity check")
        return activated

    def model_defaults_preflight(
        self, workspace_id: str, request: ModelDefaultsRequest
    ) -> ModelDefaultsPreflight:
        current = self.config(workspace_id)
        data = self._round_trip_yaml().load(current.content) or {}
        current_embedding = str(
            ((data.get("embeddings") or {}).get("model") or {}).get("name") or ""
        )
        current_provider = str(
            ((data.get("embeddings") or {}).get("model") or {}).get("provider") or ""
        )
        current_dimension = int(
            ((data.get("embeddings") or {}).get("model") or {}).get("vector_dim") or 0
        )
        requested = {
            "chat": request.chat,
            "vl": request.vl,
            "embedding": request.embedding,
            "rerank": request.rerank,
        }
        current_roles = self.configured_model_roles(workspace_id)
        changes = {
            role: f"{current_roles.get(role) or 'unset'} → {model}"
            for role, model in requested.items()
            if current_roles.get(role) != model
        }
        embedding_changed = (
            current_embedding != request.embedding
            or current_provider != request.embedding_provider
            or current_dimension != request.vector_dim
        )
        requires_reindex = embedding_changed and bool(self.documents(workspace_id))
        warnings = []
        if request.chat != request.vl:
            warnings.append(
                "Haiku uses one QA model for chat and vision; the VL model is stored as the "
                "vision preference but QA remains authoritative."
            )
        if requires_reindex:
            warnings.append(
                "Changing the embedding model or vector dimension requires a full library rebuild."
            )
        return ModelDefaultsPreflight(
            workspace_id=workspace_id,
            changes=changes,
            requires_reindex=requires_reindex,
            warnings=warnings,
        )

    def apply_model_defaults(
        self,
        workspace_id: str,
        request: ModelDefaultsRequest,
        if_match: str | None,
        *,
        profile_metadata: dict[str, object] | None = None,
    ) -> ConfigDocument:
        preflight = self.model_defaults_preflight(workspace_id, request)
        if preflight.requires_reindex:
            raise ConflictError(
                "Embedding changes on an indexed library require the dedicated rebuild workflow"
            )
        rendered = self.render_model_defaults(
            workspace_id,
            request,
            if_match,
            profile_metadata=profile_metadata,
        )
        return self.update_config(workspace_id, rendered.content, if_match)

    def configured_model_roles(self, workspace_id: str) -> dict[str, str | None]:
        settings = self.configured_model_settings(workspace_id)
        return {
            role: settings.get(role) if isinstance(settings.get(role), str) else None
            for role in ("chat", "structure", "vl", "embedding", "rerank")
        }

    def configured_model_settings(self, workspace_id: str) -> dict[str, object]:
        """Return the model identity fields that are safe to expose to planners."""

        data = self._round_trip_yaml().load(self.config(workspace_id).content) or {}
        oracle_defaults = (data.get("oracle") or {}).get("model_defaults") or {}
        qa_model = (data.get("qa") or {}).get("model") or {}
        embedding_model = (data.get("embeddings") or {}).get("model") or {}
        rerank_model = (data.get("reranking") or {}).get("model") or {}
        # qa.model is the provider-facing generator/VL model. Oracle defaults
        # are planner metadata and must never override the identity actually
        # sent to Haiku/Ollama.
        chat = qa_model.get("name") or oracle_defaults.get("chat")
        return {
            "chat": str(chat) if chat else None,
            "chat_provider": str(qa_model.get("provider") or ""),
            "chat_revision": str(qa_model.get("revision") or "") or None,
            "chat_digest": str(qa_model.get("digest") or "") or None,
            "structure": str(oracle_defaults.get("structure"))
            if oracle_defaults.get("structure")
            else None,
            "vl": str(oracle_defaults.get("vl") or chat) if chat else None,
            "embedding": str(embedding_model.get("name")) if embedding_model.get("name") else None,
            "embedding_provider": str(embedding_model.get("provider") or ""),
            "embedding_revision": str(embedding_model.get("revision") or "") or None,
            "embedding_digest": str(embedding_model.get("digest") or "") or None,
            "vector_dimension": int(embedding_model.get("vector_dim") or 0),
            "rerank": str(rerank_model.get("name")) if rerank_model.get("name") else None,
            "rerank_provider": str(rerank_model.get("provider") or ""),
            "rerank_revision": str(rerank_model.get("revision") or "") or None,
            "rerank_digest": str(rerank_model.get("digest") or "") or None,
            "profile": (data.get("oracle") or {}).get("model_profile") or {},
        }

    @staticmethod
    def _nested_model_name(data: object, section: str) -> str | None:
        if not isinstance(data, dict):
            return None
        value = ((data.get(section) or {}).get("model") or {}).get("name")
        return str(value) if value else None

    @staticmethod
    def _round_trip_yaml() -> YAML:
        yaml = YAML()
        yaml.preserve_quotes = True
        return yaml

    def create_backup(self, workspace_id: str) -> BackupSummary:
        with self._backup_lock(workspace_id):
            manifest = self.workspaces.get(workspace_id)
            workspace = Path(manifest.path)
            backup_id = f"backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"
            archive = workspace / "snapshots" / f"{backup_id}.tar.gz"
            archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            archive.parent.chmod(0o700)
            state_payload = (
                json.dumps(
                    self.store.export_workspace_index_snapshot(workspace_id),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            with tarfile.open(archive, "w:gz") as bundle:
                for item in workspace.rglob("*"):
                    relative = item.relative_to(workspace)
                    if relative.parts[0] in {"snapshots", "backup-manifests"}:
                        continue
                    if ".omarag/locks" in relative.as_posix():
                        continue
                    bundle.add(item, arcname=relative, recursive=False)
                state_info = tarfile.TarInfo(".omarag/index-state.json")
                state_info.size = len(state_payload)
                state_info.mode = 0o600
                state_info.mtime = int(datetime.now(UTC).timestamp())
                bundle.addfile(state_info, BytesIO(state_payload))
            archive.chmod(0o600)
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
            _secure_write_text(target, summary.model_dump_json(indent=2) + "\n")
            self._prune_unpinned_backups(workspace_id)
            return summary

    def _prune_unpinned_backups(self, workspace_id: str, keep: int = 3) -> None:
        workspace = Path(self.workspaces.get(workspace_id).path)
        unpinned = [item for item in self.list_backups(workspace_id) if not item.pinned]
        for expired in unpinned[keep:]:
            archive = Path(expired.path)
            if archive.parent.resolve() == (workspace / "snapshots").resolve():
                archive.unlink(missing_ok=True)
            (workspace / "backup-manifests" / f"{expired.id}.json").unlink(missing_ok=True)

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

    def set_backup_pinned(self, workspace_id: str, backup_id: str, pinned: bool) -> BackupSummary:
        with self._backup_lock(workspace_id):
            backup = next(
                (item for item in self.list_backups(workspace_id) if item.id == backup_id),
                None,
            )
            if backup is None:
                raise NotFoundError(f"Sicherung {backup_id} wurde nicht gefunden")
            updated = backup.model_copy(update={"pinned": pinned})
            workspace = Path(self.workspaces.get(workspace_id).path)
            manifest = workspace / "backup-manifests" / f"{backup_id}.json"
            _secure_write_text(manifest, updated.model_dump_json(indent=2) + "\n")
            self._prune_unpinned_backups(workspace_id)
            return updated

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
        current_state = self.store.export_workspace_index_snapshot(workspace_id)
        staging = Path(tempfile.mkdtemp(prefix=f".{workspace.name}.restore-", dir=workspace.parent))
        retired = workspace.parent / f".{workspace.name}.previous-{uuid4().hex[:8]}"
        switched = False
        state_switched = False
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
            state_path = staging / ".omarag" / "index-state.json"
            if not state_path.is_file():
                raise ConflictError(
                    "Legacy backup lacks its generation catalogue and cannot be restored safely"
                )
            try:
                restored_state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConflictError("Backup generation catalogue is invalid") from exc

            safety = self.create_backup(workspace_id)
            for persistent in ("snapshots", "backup-manifests"):
                source = workspace / persistent
                if source.exists():
                    shutil.copytree(source, staging / persistent, dirs_exist_ok=True)

            workspace.rename(retired)
            staging.rename(workspace)
            switched = True
            self.store.restore_workspace_index_snapshot(workspace_id, restored_state)
            state_switched = True
            restored_state_path = workspace / ".omarag" / "index-state.json"
            restored_state_path.unlink(missing_ok=True)
            return selected, safety
        except Exception:
            if retired.exists() and not workspace.exists():
                retired.rename(workspace)
            elif switched and retired.exists() and workspace.exists():
                failed = workspace.parent / f".{workspace.name}.failed-{uuid4().hex[:8]}"
                workspace.rename(failed)
                retired.rename(workspace)
                shutil.rmtree(failed, ignore_errors=True)
                switched = False
            if state_switched:
                self.store.restore_workspace_index_snapshot(workspace_id, current_state)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if switched and retired.exists():
                shutil.rmtree(retired, ignore_errors=True)

    @staticmethod
    def _validate_archive_members(members: list[tarfile.TarInfo]) -> None:
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ConflictError("Sicherung enthaelt einen unsicheren Pfad")
            if member.issym() or member.islnk() or member.isdev():
                raise ConflictError("Sicherung enthaelt einen nicht erlaubten Dateityp")
