from __future__ import annotations

import hashlib
import re
import shutil
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from uuid import uuid4

import tomli_w

from ..models.api import CloneWorkspaceRequest, CreateWorkspaceRequest, PatchWorkspaceRequest
from ..models.domain import WorkspaceManifest, WorkspaceSummary
from ..models.errors import ConflictError, EtagConflictError, ReadOnlyError
from ..store import StateStore


def _slug(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:48] or "workspace"


def _memory_gib() -> float:
    try:
        line = next(
            item
            for item in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
            if item.startswith("MemTotal:")
        )
        return int(line.split()[1]) / 1024**2
    except (OSError, StopIteration, ValueError, IndexError):
        return 16.0


def _runtime_profile() -> dict[str, int]:
    memory = _memory_gib()
    if memory <= 10:
        return {
            "embedding_batch": 8,
            "search_limit": 6,
            "context_chars": 4000,
            "answer_tokens": 1024,
        }
    if memory < 18:
        return {
            "embedding_batch": 16,
            "search_limit": 6,
            "context_chars": 4000,
            "answer_tokens": 1024,
        }
    if memory <= 32:
        return {
            "embedding_batch": 32,
            "search_limit": 8,
            "context_chars": 5000,
            "answer_tokens": 1536,
        }
    return {
        "embedding_batch": 64,
        "search_limit": 8,
        "context_chars": 6000,
        "answer_tokens": 2048,
    }


def _haiku_version() -> str | None:
    for distribution in ("haiku-rag", "haiku-rag-slim"):
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return None


class WorkspaceService:
    def __init__(
        self,
        root: Path,
        store: StateStore,
        ollama_url: str = "http://127.0.0.1:11434",
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.ollama_url = ollama_url.rstrip("/")

    @staticmethod
    def _etag(workspace_id: str, name: str, updated_at: datetime) -> str:
        raw = f"{workspace_id}:{name}:{updated_at.isoformat()}".encode()
        return hashlib.sha256(raw).hexdigest()[:24]

    def create(self, request: CreateWorkspaceRequest) -> WorkspaceManifest:
        workspace_id = request.id or f"ws-{_slug(request.name)}-{uuid4().hex[:6]}"
        path = (self.root / f"{workspace_id}.omarag").resolve()
        if path.parent != self.root:
            raise ConflictError("Ungueltiger Workspacepfad")
        if path.exists():
            raise ConflictError(f"Workspacepfad {path.name} existiert bereits")
        now = datetime.now(UTC)
        manifest = WorkspaceManifest(
            id=workspace_id,
            name=request.name.strip(),
            path=str(path),
            read_only=request.read_only,
            embedding_model="qwen3-embedding:0.6b",
            vector_dimension=1024,
            haiku_last_verified=_haiku_version(),
            created_at=now,
            updated_at=now,
            etag=self._etag(workspace_id, request.name, now),
        )
        self._create_layout(path)
        self._write_manifest(manifest)
        try:
            self.store.add_workspace(manifest)
        except Exception:
            shutil.rmtree(path)
            raise
        return manifest

    def _create_layout(self, path: Path) -> None:
        for relative in (
            "metadata-overlays",
            "database",
            "evaluations/history",
            "evaluations/variants",
            "annotations",
            "reports",
            "snapshots",
            "backup-manifests",
            "sources/originals",
            ".omarag/locks",
        ):
            (path / relative).mkdir(parents=True, exist_ok=True)
        runtime = _runtime_profile()
        (path / "haiku.rag.yaml").write_text(
            f"""environment: production

embeddings:
  model:
    provider: ollama
    name: qwen3-embedding:0.6b
    vector_dim: 1024
  batch_size: {runtime["embedding_batch"]}

reranking:
  model:
    provider: cross-encoder
    name: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
  multimodal: false

qa:
  model:
    provider: ollama
    name: qwen3.5:4b-q4_K_M
    vision: true
    enable_thinking: false
    temperature: 0.1
    max_tokens: {runtime["answer_tokens"]}
    extra_body:
      reasoning_effort: none
  max_searches: 3

processing:
  converter: docling-local
  chunker: docling-local
  chunker_type: hybrid
  chunk_size: 384
  chunking_tokenizer: Qwen/Qwen3-Embedding-0.6B
  chunking_merge_peers: true
  chunking_use_markdown_tables: true
  split_pages: 25
  pictures: none
  auto_title: false
  conversion_options:
    do_ocr: true
    force_ocr: false
    ocr_engine: auto
    ocr_lang:
      - de
      - en
    do_table_structure: true
    table_mode: accurate
    table_cell_matching: true
    images_scale: 1.0
    generate_page_images: false
    fetch_remote_images: false

providers:
  ollama:
    base_url: {self.ollama_url}

search:
  limit: {runtime["search_limit"]}
  max_context_chars: {runtime["context_chars"]}

prompts:
  domain_preamble: |-
    Die ausgewaehlten Fach- und Lehrbuecher sind die massgebliche Quelle.
    Uebernimm Zahlen, Einheiten und Formelzeichen exakt. Erfinde keine
    Seitenzahlen oder Fundstellen und widersprich einer Ausgabe nicht mit
    unmarkiertem Modellwissen. Reicht der Kontext nicht aus, sage das klar.
    Formatiere Tabellen als Markdown und Mathematik als LaTeX zwischen
    Dollarzeichen. Verweise im Fliesstext nicht auf Abbildungsnummern.
""",
            encoding="utf-8",
        )
        (path / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (path / ".omarag" / "queue-links.json").write_text("{}\n", encoding="utf-8")

    @staticmethod
    def _manifest_toml(manifest: WorkspaceManifest) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": manifest.schema_version,
            "id": manifest.id,
            "name": manifest.name,
            "created_at": manifest.created_at,
            "updated_at": manifest.updated_at,
            "read_only": manifest.read_only,
            "haiku": {
                "compatible_range": manifest.haiku_compatible_range,
                "update_policy": manifest.haiku_update_policy,
                "last_verified": manifest.haiku_last_verified or "",
                "database_schema_version": manifest.database_schema_version,
            },
            "embedding": {
                "provider": manifest.embedding_provider,
                "model": manifest.embedding_model,
            },
            "defaults": {
                "processing_profile": manifest.processing_profile,
                "evidence_mode": manifest.evidence_mode.value,
                "document_policy": manifest.document_policy,
            },
            "privacy": {
                "mode": manifest.privacy_mode.value,
                "cloud_acknowledged": manifest.cloud_acknowledged,
            },
        }
        embedding = data["embedding"]
        assert isinstance(embedding, dict)
        if manifest.vector_dimension is not None:
            embedding["vector_dimension"] = manifest.vector_dimension
        return data

    def _write_manifest(self, manifest: WorkspaceManifest) -> None:
        path = Path(manifest.path)
        temporary = path / "workspace.toml.tmp"
        temporary.write_text(tomli_w.dumps(self._manifest_toml(manifest)), encoding="utf-8")
        temporary.replace(path / "workspace.toml")

    def list(self) -> list[WorkspaceSummary]:
        return [
            WorkspaceSummary(
                id=item.id,
                name=item.name,
                path=item.path,
                read_only=item.read_only,
                updated_at=item.updated_at,
                etag=item.etag,
            )
            for item in self.store.list_workspaces()
        ]

    def get(self, workspace_id: str) -> WorkspaceManifest:
        return self.store.get_workspace(workspace_id)

    def patch(
        self, workspace_id: str, request: PatchWorkspaceRequest, if_match: str | None
    ) -> WorkspaceManifest:
        current = self.get(workspace_id)
        if if_match is not None and if_match.strip('"') != current.etag:
            raise EtagConflictError("Workspace wurde zwischenzeitlich geaendert")
        if current.read_only and request != PatchWorkspaceRequest(read_only=False):
            raise ReadOnlyError("Read-only-Workspace muss zuerst entsperrt werden")
        update = request.model_dump(exclude_none=True)
        now = datetime.now(UTC)
        name = str(update.get("name", current.name))
        updated = current.model_copy(
            update={
                **update,
                "updated_at": now,
                "etag": self._etag(current.id, name, now),
            }
        )
        self._write_manifest(updated)
        self.store.update_workspace(updated)
        return updated

    def clone(self, workspace_id: str, request: CloneWorkspaceRequest) -> WorkspaceManifest:
        source = self.get(workspace_id)
        target_id = request.id or f"ws-{_slug(request.name)}-{uuid4().hex[:6]}"
        target = (self.root / f"{target_id}.omarag").resolve()
        if target.parent != self.root or target.exists():
            raise ConflictError("Ziel fuer Workspace-Klon ist ungueltig oder existiert bereits")
        shutil.copytree(
            source.path,
            target,
            ignore=shutil.ignore_patterns("locks", "runtime-state.json"),
        )
        now = datetime.now(UTC)
        manifest = source.model_copy(
            update={
                "id": target_id,
                "name": request.name,
                "path": str(target),
                "created_at": now,
                "updated_at": now,
                "read_only": False,
                "etag": self._etag(target_id, request.name, now),
            }
        )
        self._write_manifest(manifest)
        self.store.add_workspace(manifest)
        return manifest

    def delete(self, workspace_id: str, physical: bool) -> None:
        manifest = self.get(workspace_id)
        path = Path(manifest.path).resolve()
        if physical and (path.parent != self.root or path.suffix != ".omarag"):
            raise ConflictError("Workspacepfad liegt ausserhalb des verwalteten Bereichs")
        retired: Path | None = None
        if physical and path.exists():
            retired = self.root / f".{path.name}.deleting-{uuid4().hex[:8]}"
            path.rename(retired)
        try:
            self.store.remove_workspace(workspace_id)
        except Exception:
            if retired is not None and retired.exists() and not path.exists():
                retired.rename(path)
            raise
        if retired is not None:
            shutil.rmtree(retired)

    def database_path(self, workspace_id: str) -> Path:
        manifest = self.get(workspace_id)
        return Path(manifest.path) / "database" / "knowledge.lancedb"
