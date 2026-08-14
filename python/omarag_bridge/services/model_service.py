from __future__ import annotations

import asyncio
import html
import json
import math
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..models.domain import (
    HardwareInfo,
    HardwareProfile,
    ModelCatalogEntry,
    ModelCatalogResponse,
    ModelCategory,
    ModelFit,
    ModelOperationResult,
    ModelPackage,
    ModelPackageItem,
    ModelResidency,
    ModelRoleRuntime,
    ModelRuntime,
    ModelRuntimeResponse,
    ModelSource,
)
from ..models.errors import ConflictError, UpstreamUnavailableError

GIB = 1024**3
MIB = 1024**2


class ModelService:
    """Hardware-aware model operations around vanilla Haiku RAG.

    Haiku remains the only RAG engine. This service owns provider lifecycle and
    catalog access so terminal clients never need to bypass the OmaRag API.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def catalog(
        self,
        source: ModelSource,
        category: ModelCategory,
        query: str,
        quantization: str,
        context_tokens: int,
        profile: HardwareProfile,
    ) -> ModelCatalogResponse:
        hardware = self.hardware()
        installed = await self._installed_entries()
        installed_names = {entry.id for entry in installed}
        if source == ModelSource.INSTALLED:
            raw = installed
            scanned = len(raw)
            truncated = False
        elif source == ModelSource.OLLAMA:
            raw, scanned = await self._ollama_entries(installed_names)
            truncated = False
        else:
            raw, scanned, truncated = await self._hugging_face_entries(query, installed_names)

        normalized_query = query.strip().casefold()
        entries: list[ModelCatalogEntry] = []
        for entry in raw:
            if entry.category != category:
                continue
            if normalized_query and normalized_query not in (
                f"{entry.id} {entry.description} {' '.join(entry.capabilities)}".casefold()
            ):
                continue
            estimate = self.estimated_memory(entry, quantization, context_tokens)
            fit = self.fit(estimate, hardware)
            if fit is None:
                continue
            entry.estimated_memory = estimate
            entry.fit = fit
            entries.append(entry)

        self._rank_recommendations(entries, category, profile)
        entries.sort(
            key=lambda item: (
                item.recommended_rank is None,
                item.recommended_rank or 99,
                -(item.downloads or 0),
                item.id.casefold(),
            )
        )
        return ModelCatalogResponse(
            entries=entries,
            packages=self._recommended_packages(
                hardware,
                installed_names,
                quantization,
                context_tokens,
                profile,
            ),
            hardware=hardware,
            scanned=scanned,
            compatible=len(entries),
            truncated=truncated,
        )

    async def runtime(
        self,
        roles: dict[str, str | None] | None = None,
        *,
        active_roles: set[ModelCategory] | None = None,
        worker_timeout_seconds: float = 0.0,
        memory_state: str = "ready",
        worker_expires_in_seconds: float = 0.0,
    ) -> ModelRuntimeResponse:
        payload = await self._ollama_json("GET", "/api/ps")
        models = []
        for raw in payload.get("models", []):
            details = raw.get("details") or {}
            models.append(
                ModelRuntime(
                    name=raw.get("name") or raw.get("model") or "",
                    digest=str(raw.get("digest") or ""),
                    size=int(raw.get("size") or 0),
                    size_vram=int(raw.get("size_vram") or 0),
                    context_length=int(raw.get("context_length") or 0),
                    expires_at=(str(raw["expires_at"]) if raw.get("expires_at") else None),
                    capabilities=list(raw.get("capabilities") or []),
                    parameter_size=str(details.get("parameter_size") or ""),
                    quantization_level=str(details.get("quantization_level") or ""),
                )
            )
        role_rows = []
        configured = roles or {}
        active = active_roles or set()
        for role in ModelCategory:
            model = configured.get(role.value)
            loaded = bool(
                model
                and any(
                    item.name.removesuffix(":latest") == model.removesuffix(":latest")
                    for item in models
                )
            )
            shared = [
                other
                for other in ModelCategory
                if other != role and model and configured.get(other.value) == model
            ]
            role_rows.append(
                ModelRoleRuntime(
                    role=role,
                    model=model,
                    provider=("cross-encoder" if role == ModelCategory.RERANK else "ollama")
                    if model
                    else None,
                    residency=(
                        ModelResidency.ACTIVE
                        if role in active
                        else ModelResidency.LOADED
                        if loaded
                        else ModelResidency.IDLE
                        if model
                        else ModelResidency.UNCONFIGURED
                    ),
                    shared_with=shared,
                )
            )
        return ModelRuntimeResponse(
            models=models,
            roles=role_rows,
            query_worker_state="active" if active else "idle",
            query_worker_timeout_seconds=worker_timeout_seconds,
            residency_policy="adaptive",
            memory_state=memory_state,
            worker_expires_in_seconds=worker_expires_in_seconds,
        )

    async def import_gguf(
        self,
        path: Path,
        filename: str,
        model: str,
        category: ModelCategory,
        digest: str,
    ) -> AsyncIterator[bytes]:
        if category == ModelCategory.RERANK:
            raise ConflictError("Rerank requires a cross-encoder; Ollama GGUF is unsupported")
        yield self._progress_line("uploading verified GGUF", 0, path.stat().st_size)

        async def file_body() -> AsyncIterator[bytes]:
            with path.open("rb") as stream:
                while chunk := await asyncio.to_thread(stream.read, 8 * MIB):
                    yield chunk

        timeout = httpx.Timeout(connect=10, read=None, write=None, pool=10)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                blob_response = await client.post(
                    f"{self.settings.ollama_url.rstrip('/')}/api/blobs/sha256:{digest}",
                    content=file_body(),
                    headers={"Content-Type": "application/octet-stream"},
                )
                blob_response.raise_for_status()
                yield self._progress_line(
                    "creating Ollama model", path.stat().st_size, path.stat().st_size
                )
                async with client.stream(
                    "POST",
                    f"{self.settings.ollama_url.rstrip('/')}/api/create",
                    json={
                        "model": model,
                        "files": {filename: f"sha256:{digest}"},
                        "stream": True,
                    },
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            yield line.encode() + b"\n"
        except httpx.HTTPError as exc:
            yield self._error_line(f"Ollama GGUF import failed: {exc}")
            return

        try:
            details = await self._ollama_json("POST", "/api/show", {"model": model})
            capabilities = {str(item) for item in details.get("capabilities") or []}
            if category == ModelCategory.VL and "vision" not in capabilities:
                raise ConflictError("The imported GGUF does not advertise vision capability")
            if category == ModelCategory.EMBEDDING:
                await self._ollama_json("POST", "/api/embed", {"model": model, "input": "probe"})
        except Exception as exc:
            cleanup = ""
            try:
                await self.delete(model)
            except Exception as cleanup_error:
                cleanup = f"; automatic cleanup also failed: {cleanup_error}"
            yield self._error_line(
                f"Role validation failed; imported model rejected: {exc}{cleanup}"
            )
            return
        yield self._progress_line("success", path.stat().st_size, path.stat().st_size)

    @staticmethod
    def _progress_line(status: str, completed: int = 0, total: int = 0) -> bytes:
        return (
            json.dumps({"status": status, "completed": completed, "total": total}) + "\n"
        ).encode()

    async def pull(self, model: str) -> AsyncIterator[bytes]:
        timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)
        try:
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream(
                    "POST",
                    f"{self.settings.ollama_url.rstrip('/')}/api/pull",
                    json={"model": model, "stream": True},
                ) as response,
            ):
                if response.is_error:
                    body = await response.aread()
                    yield self._error_line(
                        f"Ollama pull failed with HTTP {response.status_code}: "
                        f"{body.decode(errors='replace')}"
                    )
                    return
                async for line in response.aiter_lines():
                    if line:
                        yield line.encode() + b"\n"
        except httpx.HTTPError as exc:
            yield self._error_line(f"Ollama is unavailable: {exc}")

    async def load(self, model: str, context_tokens: int, keep_alive: str) -> ModelOperationResult:
        await self._ollama_json(
            "POST",
            "/api/chat",
            {
                "model": model,
                "messages": [],
                "stream": False,
                "keep_alive": keep_alive,
                "options": {"num_ctx": context_tokens},
            },
        )
        return ModelOperationResult(model=model, operation="load", status="ok")

    async def unload(self, model: str) -> ModelOperationResult:
        await self._ollama_json(
            "POST",
            "/api/chat",
            {"model": model, "messages": [], "stream": False, "keep_alive": 0},
        )
        return ModelOperationResult(model=model, operation="unload", status="ok")

    async def warm_embedding(self, model: str, keep_alive: str = "120s") -> None:
        # Ollama's embedding endpoint requires real input; a tiny fixed probe is
        # deterministic and avoids pretending that reranking has been warmed.
        await self._ollama_json(
            "POST",
            "/api/embed",
            {"model": model, "input": "OmaRag warmup", "keep_alive": keep_alive},
        )

    async def delete(self, model: str) -> ModelOperationResult:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(
                    "DELETE",
                    f"{self.settings.ollama_url.rstrip('/')}/api/delete",
                    json={"model": model},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"Ollama model deletion failed: {exc}") from exc
        return ModelOperationResult(model=model, operation="delete", status="ok")

    async def _installed_entries(self) -> list[ModelCatalogEntry]:
        payload = await self._ollama_json("GET", "/api/tags")
        result = []
        for raw in payload.get("models", []):
            details = raw.get("details") or {}
            model_id = raw.get("name") or raw.get("model") or ""
            parameter_count = self._parameter_count(str(details.get("parameter_size") or ""))
            category = self._category(model_id, [], "")
            result.append(
                ModelCatalogEntry(
                    id=model_id,
                    source=ModelSource.INSTALLED,
                    category=category,
                    description=f"Installed {details.get('parameter_size') or ''} model".strip(),
                    parameter_count=parameter_count,
                    estimated_size=int(raw.get("size") or 0),
                    estimated_memory=int(raw.get("size") or 0),
                    installed=True,
                    quantization=details.get("quantization_level"),
                    fit=ModelFit.TIGHT,
                    capabilities=[],
                )
            )
        return result

    async def _ollama_entries(
        self, installed_names: set[str]
    ) -> tuple[list[ModelCatalogEntry], int]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get("https://ollama.com/library")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"Ollama library is unavailable: {exc}") from exc

        entries: list[ModelCatalogEntry] = []
        scanned = 0
        for block in re.findall(
            r'<li[^>]*class="[^"]*border-b[^"]*"[^>]*>(.*?)</li>',
            response.text,
            re.DOTALL,
        ):
            match = re.search(r'href="/library/([^"/?]+)', block)
            if not match:
                continue
            family = html.unescape(match.group(1))
            description_match = re.search(r'<p class="max-w-lg[^"]*">(.*?)</p>', block, re.DOTALL)
            description = self._plain_text(description_match.group(1)) if description_match else ""
            tags = [
                self._plain_text(tag).casefold()
                for tag in re.findall(
                    r'<span[^>]*class="[^"]*rounded-md[^"]*"[^>]*>(.*?)</span>',
                    block,
                    re.DOTALL,
                )
            ]
            pulls_match = re.search(
                r'<span[^>]*>([^<]+)</span>\s*<span class="hidden sm:flex">&nbsp;Pulls</span>',
                block,
            )
            downloads = self._compact_count(pulls_match.group(1)) if pulls_match else None
            variants = [tag for tag in tags if re.fullmatch(r"\d+(?:\.\d+)?[bm]", tag)]
            if not variants:
                inferred = self._parameter_label(f"{family} {description}")
                variants = [inferred] if inferred else []
            category = self._category(family, tags, description)
            capabilities = [
                tag for tag in tags if tag in {"vision", "embedding", "tools", "thinking"}
            ]
            for variant in variants:
                scanned += 1
                model_id = f"{family}:{variant}"
                entries.append(
                    ModelCatalogEntry(
                        id=model_id,
                        source=ModelSource.OLLAMA,
                        category=category,
                        description=description,
                        downloads=downloads,
                        parameter_count=self._parameter_count(variant),
                        estimated_memory=0,
                        installed=any(
                            name == model_id or name.startswith(f"{model_id}-")
                            for name in installed_names
                        ),
                        fit=ModelFit.TIGHT,
                        capabilities=capabilities,
                    )
                )
        return entries, scanned

    async def _hugging_face_entries(
        self, query: str, installed_names: set[str]
    ) -> tuple[list[ModelCatalogEntry], int, bool]:
        limit = max(50, min(self.settings.model_catalog_scan_limit, 1000))
        params: list[tuple[str, str]] = [
            ("filter", "gguf"),
            ("sort", "downloads"),
            ("direction", "-1"),
            ("limit", str(limit)),
        ]
        params.extend(
            ("expand[]", field) for field in ("downloads", "likes", "tags", "gguf", "pipeline_tag")
        )
        if query.strip():
            params.append(("search", query.strip()))
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.settings.hugging_face_url.rstrip('/')}/api/models",
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamUnavailableError(f"Hugging Face Hub is unavailable: {exc}") from exc

        entries = []
        for raw in payload:
            model_id = str(raw.get("id") or "")
            tags = list(raw.get("tags") or [])
            pipeline = str(raw.get("pipeline_tag") or "")
            category = self._category(model_id, tags + [pipeline], "")
            gguf = raw.get("gguf") or {}
            parameters = gguf.get("total")
            if not isinstance(parameters, int) or parameters <= 0:
                continue
            capabilities = [
                value
                for value in (pipeline, *tags)
                if value
                in {
                    "text-generation",
                    "image-text-to-text",
                    "feature-extraction",
                    "sentence-similarity",
                    "text-classification",
                }
            ][:4]
            entries.append(
                ModelCatalogEntry(
                    id=model_id,
                    source=ModelSource.HUGGING_FACE,
                    category=category,
                    description=(
                        f"GGUF · {pipeline or category.value}"
                        + (f" · {', '.join(capabilities[:2])}" if capabilities else "")
                    ),
                    likes=raw.get("likes"),
                    downloads=raw.get("downloads"),
                    parameter_count=parameters,
                    estimated_size=(gguf.get("totalFileSize") if isinstance(gguf, dict) else None),
                    estimated_memory=0,
                    installed=any(
                        model_id.casefold() in name.casefold() for name in installed_names
                    ),
                    fit=ModelFit.TIGHT,
                    capabilities=capabilities,
                )
            )
        return entries, len(payload), len(payload) >= limit

    @staticmethod
    def hardware() -> HardwareInfo:
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        hardware = HardwareInfo(
            memory_total=values.get("MemTotal", 0),
            memory_available=values.get("MemAvailable", 0),
        )
        for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
            device = card / "device"
            total = ModelService._read_int(device / "mem_info_vram_total")
            if not total:
                continue
            hardware.vram_total = total
            hardware.vram_used = ModelService._read_int(device / "mem_info_vram_used")
            hardware.shared_memory = ModelService._read_int(device / "mem_info_gtt_total")
            vendor = ModelService._read_text(device / "vendor")
            device_id = ModelService._read_text(device / "device")
            hardware.gpu = {
                ("0x1002", "0x1900"): "AMD Radeon 760M",
            }.get((vendor, device_id), f"GPU {device_id or card.name}")
            break
        return hardware

    @staticmethod
    def estimated_memory(entry: ModelCatalogEntry, quantization: str, context_tokens: int) -> int:
        if entry.source == ModelSource.INSTALLED and entry.estimated_size:
            weights = entry.estimated_size
        elif entry.parameter_count:
            bits = {
                "Q3_K_M": 3.4,
                "Q4_K_M": 4.5,
                "Q5_K_M": 5.5,
                "Q6_K": 6.5,
                "Q8_0": 8.5,
            }.get(quantization.upper(), 4.5)
            weights = int(entry.parameter_count * bits / 8 * 1.08)
        else:
            return 0
        billions = (entry.parameter_count or 1_000_000_000) / 1e9
        if entry.category in {ModelCategory.EMBEDDING, ModelCategory.RERANK}:
            reserve = int(billions * 32 * MIB) + 128 * MIB
        else:
            reserve = int(billions * context_tokens / 8192 * 80 * MIB) + 256 * MIB
        return weights + reserve

    @staticmethod
    def fit(estimate: int, hardware: HardwareInfo) -> ModelFit | None:
        if estimate <= 0 or hardware.memory_total <= 0:
            return None
        available = hardware.memory_available or hardware.memory_total
        usable = min(available, int(hardware.memory_total * 0.72)) - 2 * GIB
        if usable <= 0 or estimate > usable:
            return None
        if estimate <= int(usable * 0.68):
            return ModelFit.COMFORTABLE
        return ModelFit.TIGHT

    def _recommended_packages(
        self,
        hardware: HardwareInfo,
        installed_names: set[str],
        quantization: str,
        context_tokens: int,
        profile: HardwareProfile,
    ) -> list[ModelPackage]:
        qwen_size = {
            HardwareProfile.ECO: ("2b", 2_000_000_000),
            HardwareProfile.LAPTOP: ("2b", 2_000_000_000),
            HardwareProfile.QUALITY: ("4b", 4_000_000_000),
        }[profile]
        deeper_qwen_size = {
            HardwareProfile.ECO: ("2b", 2_000_000_000),
            HardwareProfile.LAPTOP: ("4b", 4_000_000_000),
            HardwareProfile.QUALITY: ("4b", 4_000_000_000),
        }[profile]
        templates = [
            (
                "qwen-unified",
                "Fast",
                "Lowest memory footprint with complete chat, vision and retrieval roles.",
                "Qwen generation, embeddings and reranking share one tokenizer family.",
                [
                    (
                        ModelCategory.CHAT,
                        f"qwen3.5:{qwen_size[0]}",
                        ModelSource.OLLAMA,
                        qwen_size[1],
                    ),
                    (ModelCategory.VL, f"qwen3.5:{qwen_size[0]}", ModelSource.OLLAMA, qwen_size[1]),
                    (
                        ModelCategory.EMBEDDING,
                        "qwen3-embedding:0.6b",
                        ModelSource.OLLAMA,
                        600_000_000,
                    ),
                    (
                        ModelCategory.RERANK,
                        "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
                        ModelSource.HUGGING_FACE,
                        600_000_000,
                    ),
                ],
            ),
            (
                "qwen-depth",
                "Balanced",
                "The recommended balance of answer quality, speed and memory.",
                "The same Qwen embedding/rerank pair avoids cross-family retrieval drift.",
                [
                    (
                        ModelCategory.CHAT,
                        f"qwen3.5:{deeper_qwen_size[0]}",
                        ModelSource.OLLAMA,
                        deeper_qwen_size[1],
                    ),
                    (
                        ModelCategory.VL,
                        f"qwen3.5:{deeper_qwen_size[0]}",
                        ModelSource.OLLAMA,
                        deeper_qwen_size[1],
                    ),
                    (
                        ModelCategory.EMBEDDING,
                        "qwen3-embedding:0.6b",
                        ModelSource.OLLAMA,
                        600_000_000,
                    ),
                    (
                        ModelCategory.RERANK,
                        "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
                        ModelSource.HUGGING_FACE,
                        600_000_000,
                    ),
                ],
            ),
            (
                "bge-retrieval",
                "Quality",
                "Stronger generation and reranking while preserving the current vector index.",
                "A larger cross-encoder improves ranking without forcing an embedding rebuild.",
                [
                    (ModelCategory.CHAT, "qwen3.5:4b", ModelSource.OLLAMA, 4_000_000_000),
                    (ModelCategory.VL, "qwen3.5:4b", ModelSource.OLLAMA, 4_000_000_000),
                    (
                        ModelCategory.EMBEDDING,
                        "qwen3-embedding:0.6b",
                        ModelSource.OLLAMA,
                        600_000_000,
                    ),
                    (
                        ModelCategory.RERANK,
                        "BAAI/bge-reranker-v2-m3",
                        ModelSource.HUGGING_FACE,
                        568_000_000,
                    ),
                ],
            ),
        ]
        packages: list[ModelPackage] = []
        for package_id, name, summary, synergy, raw_models in templates:
            unique_models: dict[str, tuple[ModelCategory, str, ModelSource, int]] = {}
            for role, model, source, parameters in raw_models:
                unique_models.setdefault(model, (role, model, source, parameters))
            total = 0
            for role, model, source, parameters in unique_models.values():
                total += self.estimated_memory(
                    ModelCatalogEntry(
                        id=model,
                        source=source,
                        category=role,
                        parameter_count=parameters,
                        estimated_memory=0,
                        fit=ModelFit.TIGHT,
                    ),
                    quantization,
                    context_tokens,
                )
            package_fit = self.fit(total, hardware)
            if package_fit is None:
                continue
            items = []
            for role, model, source, _parameters in raw_models:
                download_name = self._package_download_name(model, source, quantization)
                items.append(
                    ModelPackageItem(
                        role=role,
                        model=model,
                        download_name=download_name,
                        source=source,
                        installed=any(
                            installed == model
                            or installed.startswith(f"{model}-")
                            or model.casefold() in installed.casefold()
                            for installed in installed_names
                        ),
                    )
                )
            packages.append(
                ModelPackage(
                    id=package_id,
                    name=name,
                    summary=summary,
                    synergy=synergy,
                    recommended_rank=len(packages) + 1,
                    total_estimated_memory=total,
                    fit=package_fit,
                    models=items,
                )
            )
        return packages[:3]

    @staticmethod
    def _package_download_name(model: str, source: ModelSource, quantization: str) -> str:
        if source == ModelSource.OLLAMA:
            return model
        return model

    @staticmethod
    def _rank_recommendations(
        entries: list[ModelCatalogEntry], category: ModelCategory, profile: HardwareProfile
    ) -> None:
        targets = {
            ModelCategory.CHAT: {"eco": 1.0, "laptop": 2.0, "quality": 4.0},
            ModelCategory.VL: {"eco": 1.0, "laptop": 2.0, "quality": 4.0},
            ModelCategory.EMBEDDING: {"eco": 0.3, "laptop": 0.6, "quality": 1.5},
            ModelCategory.RERANK: {"eco": 0.3, "laptop": 0.6, "quality": 1.5},
        }
        target = targets[category].get(profile, targets[category]["laptop"])

        unsafe_or_niche = (
            "uncensored",
            "abliterated",
            "roleplay",
            "erp",
            "nsfw",
            "experimental",
            "random",
            "claude",
            "opus",
        )

        def score(entry: ModelCatalogEntry) -> float:
            model_id = entry.id.casefold()
            billions = (entry.parameter_count or 0) / 1e9
            popularity = math.log10(max(entry.downloads or 0, 1)) * 4
            trust = math.log10(max(entry.likes or 0, 1)) * 2
            size_score = 18 - abs(billions - target) * 6
            fit_bonus = 8 if entry.fit == ModelFit.COMFORTABLE else 0
            known = (
                3
                if any(
                    name in model_id for name in ("qwen", "gemma", "llama", "nomic", "bge", "mxbai")
                )
                else 0
            )
            trusted_publishers = (
                "qwen/",
                "google/",
                "meta-llama/",
                "microsoft/",
                "mistralai/",
                "ibm-granite/",
                "nomic-ai/",
                "baai/",
                "mixedbread-ai/",
                "jinaai/",
                "sentence-transformers/",
                "ggml-org/",
            )
            publisher_bonus = 12 if model_id.startswith(trusted_publishers) else 0
            if entry.source == ModelSource.OLLAMA:
                publisher_bonus = 8
            specialty_penalty = (
                15 if any(value in model_id for value in ("docling", "ocr", "caption")) else 0
            )
            return (
                popularity
                + trust
                + size_score
                + fit_bonus
                + known
                + publisher_bonus
                - specialty_penalty
            )

        eligible = [
            entry
            for entry in entries
            if not any(value in entry.id.casefold() for value in unsafe_or_niche)
        ]
        for rank, entry in enumerate(sorted(eligible, key=score, reverse=True)[:3], 1):
            entry.recommended_rank = rank

    async def _ollama_json(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(
                    method,
                    f"{self.settings.ollama_url.rstrip('/')}{path}",
                    json=body,
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamUnavailableError(f"Ollama is unavailable: {exc}") from exc

    @staticmethod
    def _category(model_id: str, tags: list[str], description: str) -> ModelCategory:
        text = f"{model_id} {' '.join(tags)} {description}".casefold()
        identifier = model_id.casefold()
        if "rerank" in identifier or "cross-encoder" in identifier:
            return ModelCategory.RERANK
        embedding_markers = (
            "embedding",
            "embed",
            "bge-m3",
            "feature-extraction",
            "sentence-similarity",
        )
        if any(value in text for value in embedding_markers):
            return ModelCategory.EMBEDDING
        if "rerank" in text or "cross-encoder" in text:
            return ModelCategory.RERANK
        if any(
            value in text
            for value in (
                "vision",
                "image-text",
                "qwen3.5",
                "-vl",
                "vl-",
                "llava",
                "minicpm-v",
                "moondream",
                "granite-docling",
                "ocr",
            )
        ):
            return ModelCategory.VL
        return ModelCategory.CHAT

    @staticmethod
    def _parameter_count(value: str) -> int | None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*([bm])", value.casefold())
        if not match:
            return None
        multiplier = 1_000_000_000 if match.group(2) == "b" else 1_000_000
        return int(float(match.group(1)) * multiplier)

    @staticmethod
    def _parameter_label(value: str) -> str | None:
        match = re.search(r"(?:^|[^a-z0-9])(\d+(?:\.\d+)?[bm])(?:[^a-z0-9]|$)", value.casefold())
        return match.group(1) if match else None

    @staticmethod
    def _compact_count(value: str) -> int | None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*([kmb]?)", value.strip().casefold())
        if not match:
            return None
        multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
        return int(float(match.group(1)) * multiplier[match.group(2)])

    @staticmethod
    def _plain_text(value: str) -> str:
        return " ".join(html.unescape(re.sub(r"<[^>]+>", "", value)).split())

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text().strip().casefold()
        except OSError:
            return ""

    @staticmethod
    def _read_int(path: Path) -> int:
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _error_line(message: str) -> bytes:
        return (json.dumps({"error": message}) + "\n").encode()
