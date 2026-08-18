from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import math
import os
import platform
import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..models.domain import (
    AcceleratorInfo,
    CatalogArtifactStatus,
    CatalogProvider,
    CatalogRole,
    HardwareBenchmark,
    HardwareClassification,
    HardwareInfo,
    HardwareProfile,
    HardwareProfileView,
    HardwareReadiness,
    HardwareTier,
    ModelArtifact,
    ModelAssignment,
    ModelCatalogEntry,
    ModelCatalogManifest,
    ModelCatalogResponse,
    ModelCategory,
    ModelFit,
    ModelInstallState,
    ModelOperationResult,
    ModelPackage,
    ModelPackageItem,
    ModelProfilePreflight,
    ModelResidency,
    ModelRoleRuntime,
    ModelRuntime,
    ModelRuntimeResponse,
    ModelSource,
    ModelStackRecommendation,
    ModelTierDefinition,
    PerformanceProfile,
    SimpleModelRecommendation,
)
from ..models.errors import ConflictError, UpstreamUnavailableError

GIB = 1024**3
MIB = 1024**2
CATALOG_FILE = "model_catalog_2026_08.json"
CATALOG_CHECKSUM_FILE = f"{CATALOG_FILE}.sha256"
BENCHMARK_CACHE_FILE = "hardware-benchmark-v1.json"


class ModelService:
    """Hardware-aware model operations around vanilla Haiku RAG.

    Haiku remains the only RAG engine. This service owns provider lifecycle and
    catalog access so terminal clients never need to bypass the OmaRag API.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _hardware_fingerprint(hardware: HardwareInfo) -> str:
        """Fingerprint stable device capacity, excluding momentary free memory."""

        payload = {
            "platform": hardware.platform.casefold(),
            "architecture": hardware.architecture.casefold(),
            "cpu_model": hardware.cpu_model,
            "logical_cores": hardware.logical_cores,
            "physical_cores": hardware.physical_cores,
            "memory_capacity": hardware.memory_capacity or hardware.memory_total,
            "dedicated_vram_total": (hardware.dedicated_vram_total or hardware.vram_total),
            "accelerators": [
                {
                    "id": item.id,
                    "name": item.name,
                    "vendor": item.vendor,
                    "device_id": item.device_id,
                    "driver": item.driver,
                    "integrated": item.integrated,
                    "dedicated_memory_total": item.dedicated_memory_total,
                    "backends": sorted(item.backends),
                }
                for item in hardware.accelerators
            ],
        }
        material = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode()).hexdigest()

    def _load_cached_benchmark(
        self,
        hardware: HardwareInfo,
        profile: PerformanceProfile,
        *,
        catalog_release: str,
    ) -> HardwareBenchmark | None:
        path = self.settings.data_dir / BENCHMARK_CACHE_FILE
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 2:
                return None
            key = self._benchmark_cache_key(hardware, profile, catalog_release)
            result = HardwareBenchmark.model_validate((payload.get("entries") or {}).get(key))
            if datetime.now(UTC) - result.measured_at > timedelta(days=30):
                return None
            manifest = self.curated_catalog()
            definition = self._tier_definition(manifest, result.tested_tier)
            artifacts = {artifact.id: artifact for artifact in manifest.artifacts}
            expected = {
                CatalogRole.CHAT.value: artifacts[definition.generator].digest,
                CatalogRole.EMBEDDING.value: artifacts[definition.embedding].digest,
            }
            if result.model_digests != expected:
                return None
            return result
        except (OSError, TypeError, ValueError):
            return None

    def _benchmark_cache_key(
        self,
        hardware: HardwareInfo,
        profile: PerformanceProfile,
        catalog_release: str,
    ) -> str:
        material = "|".join((catalog_release, self._hardware_fingerprint(hardware), profile.value))
        return hashlib.sha256(material.encode()).hexdigest()

    def _save_cached_benchmark(
        self,
        hardware: HardwareInfo,
        profile: PerformanceProfile,
        result: HardwareBenchmark,
        *,
        catalog_release: str,
    ) -> None:
        path = self.settings.data_dir / BENCHMARK_CACHE_FILE
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            entries = (
                dict(existing.get("entries") or {}) if existing.get("schema_version") == 2 else {}
            )
        except (OSError, TypeError, ValueError):
            entries = {}
        key = self._benchmark_cache_key(hardware, profile, catalog_release)
        entries[key] = result.model_dump(mode="json")
        payload = {"schema_version": 2, "entries": entries}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)

    @staticmethod
    @lru_cache(maxsize=1)
    def curated_catalog() -> ModelCatalogManifest:
        """Load and integrity-check the release-bound catalog from the wheel."""

        package = resources.files("omarag_bridge.catalog")
        raw = package.joinpath(CATALOG_FILE).read_bytes()
        checksum_line = package.joinpath(CATALOG_CHECKSUM_FILE).read_text(encoding="ascii")
        expected = checksum_line.strip().split()[0].casefold()
        actual = hashlib.sha256(raw).hexdigest()
        if not expected or not hmac.compare_digest(actual, expected):
            raise RuntimeError(
                f"Embedded model catalog checksum mismatch: expected {expected}, got {actual}"
            )
        return ModelCatalogManifest.model_validate_json(raw)

    @staticmethod
    def performance_profile(
        profile: PerformanceProfile | HardwareProfile | str,
    ) -> PerformanceProfile:
        if isinstance(profile, PerformanceProfile):
            return profile
        aliases = {
            HardwareProfile.ECO.value: PerformanceProfile.FAST,
            HardwareProfile.LAPTOP.value: PerformanceProfile.NORMAL,
            HardwareProfile.QUALITY.value: PerformanceProfile.QUALITY,
            "balanced": PerformanceProfile.NORMAL,
            "deep": PerformanceProfile.QUALITY,
            "auto": PerformanceProfile.NORMAL,
        }
        normalized = str(profile).strip().casefold()
        if normalized in aliases:
            return aliases[normalized]
        return PerformanceProfile(normalized)

    @staticmethod
    def classify_hardware(
        hardware: HardwareInfo,
        benchmark: HardwareBenchmark | None = None,
    ) -> HardwareClassification:
        capacity_ram, capacity_vram, available_ram, available_vram = (
            ModelService._classification_resources(hardware)
        )
        capacity = ModelService._tier_from_resources(
            capacity_ram,
            capacity_vram,
        )
        readiness_tier = min(
            capacity,
            ModelService._tier_from_resources(available_ram, available_vram),
        )
        factors: list[str] = []
        readiness = HardwareReadiness.READY
        if hardware.platform.casefold() != "linux" or hardware.architecture.casefold() not in {
            "x86_64",
            "amd64",
        }:
            readiness = HardwareReadiness.UNSUPPORTED
            factors.append("V1.1 supports Linux x86_64 only")
        else:
            ram_ratio = available_ram / capacity_ram if capacity_ram else 0.0
            vram_ratio = available_vram / capacity_vram if capacity_vram else 1.0
            if ram_ratio < 0.20 or vram_ratio < 0.15:
                readiness = HardwareReadiness.CONSTRAINED
                factors.append("too little memory is currently free")
            elif ram_ratio < 0.35 or vram_ratio < 0.30:
                readiness = HardwareReadiness.GUARDED
                factors.append("background workloads reduce current model headroom")
            if hardware.storage_available and hardware.storage_available < 5 * GIB:
                readiness = HardwareReadiness.CONSTRAINED
                factors.append("less than 5 GiB storage is currently free")
            tier_gap = capacity.value - readiness_tier.value
            if tier_gap >= 3:
                readiness = HardwareReadiness.CONSTRAINED
                factors.append("current free memory is several tiers below device capacity")
            elif tier_gap and readiness == HardwareReadiness.READY:
                readiness = HardwareReadiness.GUARDED
                factors.append("current free memory is below the persistent device tier")
        if capacity_vram == 0:
            factors.append("no dedicated model VRAM detected; CPU inference is used")
        if hardware.logical_cores and hardware.logical_cores < 4:
            factors.append("fewer than four logical CPU cores may limit throughput")

        performance_tier = benchmark.performance_tier if benchmark is not None else None
        effective_value = min(
            capacity.value,
            performance_tier.value if performance_tier is not None else capacity.value,
        )
        return HardwareClassification(
            capacity_tier=capacity,
            readiness_tier=readiness_tier,
            performance_tier=performance_tier,
            effective_tier=HardwareTier(effective_value),
            readiness=readiness,
            limiting_factors=list(dict.fromkeys([*hardware.limiting_factors, *factors])),
            benchmark_required=benchmark is None,
        )

    @staticmethod
    def _classification_resources(hardware: HardwareInfo) -> tuple[int, int, int, int]:
        """Return capacity/readiness RAM and dedicated VRAM without UMA double-counting."""

        available_ram = hardware.memory_available or hardware.memory_total
        if not hardware.accelerators:
            capacity_ram = hardware.memory_capacity or hardware.memory_total
            capacity_vram = hardware.dedicated_vram_total or hardware.vram_total
            available_vram = max(
                capacity_vram - (hardware.dedicated_vram_used or hardware.vram_used), 0
            )
            return capacity_ram, capacity_vram, available_ram, available_vram

        integrated_reserved = max(
            (item.dedicated_memory_total for item in hardware.accelerators if item.integrated),
            default=0,
        )
        integrated_free = max(
            (
                max(item.dedicated_memory_total - item.dedicated_memory_used, 0)
                for item in hardware.accelerators
                if item.integrated
            ),
            default=0,
        )
        discrete = [item for item in hardware.accelerators if not item.integrated]
        capacity_vram = max(
            (item.dedicated_memory_total for item in discrete),
            default=hardware.dedicated_vram_total,
        )
        available_vram = max(
            (max(item.dedicated_memory_total - item.dedicated_memory_used, 0) for item in discrete),
            default=max(capacity_vram - hardware.dedicated_vram_used, 0),
        )
        capacity_ram = max(
            hardware.memory_capacity,
            hardware.memory_total + integrated_reserved,
        )
        return (
            capacity_ram,
            capacity_vram,
            min(available_ram + integrated_free, capacity_ram),
            available_vram,
        )

    async def recommend(
        self,
        profile: PerformanceProfile | HardwareProfile | str = PerformanceProfile.NORMAL,
        *,
        hardware: HardwareInfo | None = None,
        benchmark: HardwareBenchmark | None = None,
        tier_override: HardwareTier | int | None = None,
        use_cached_benchmark: bool = True,
    ) -> ModelStackRecommendation:
        """Recommend a pinned stack without downloading or changing configuration."""

        manifest = self.curated_catalog()
        selected_profile = self.performance_profile(profile)
        snapshot = hardware or self.hardware(self.settings.data_dir)
        if benchmark is None and use_cached_benchmark:
            benchmark = self._load_cached_benchmark(
                snapshot,
                selected_profile,
                catalog_release=manifest.release,
            )
        classification = self.classify_hardware(snapshot, benchmark)
        tier = HardwareTier(tier_override or classification.effective_tier)
        if tier.value > classification.capacity_tier.value:
            tier = classification.capacity_tier

        definition = self._tier_definition(manifest, tier)
        artifacts = {artifact.id: artifact for artifact in manifest.artifacts}
        warnings = list(classification.limiting_factors)
        if classification.benchmark_required:
            warnings.append("hardware tier is provisional until the local canary passes")
        elif benchmark is not None:
            if not benchmark.passed:
                warnings.append(
                    "the local canary missed its latency floor; a lower stack tier is used"
                )
            warnings.extend(f"local canary: {issue}" for issue in benchmark.issues)

        platform_id = f"{snapshot.platform.casefold()}-{snapshot.architecture.casefold()}"
        platform_id = platform_id.replace("amd64", "x86_64")
        if platform_id not in manifest.supported_platforms:
            warnings.append(f"catalog does not support {platform_id}")

        available_backends = {"cpu"}
        for accelerator in snapshot.accelerators:
            available_backends.update(accelerator.backends)

        generator = artifacts[definition.generator]
        if generator.status == CatalogArtifactStatus.REVOKED:
            generator = self._safe_fallback_generator(definition, artifacts)
            warnings.append("primary generator is revoked; pinned fallback selected")

        visual: ModelArtifact | None = None
        if definition.visual_embedding:
            candidate = artifacts[definition.visual_embedding]
            if (
                candidate.status != CatalogArtifactStatus.REVOKED
                and available_backends.intersection(candidate.backends)
            ):
                visual = candidate
            elif definition.fallback_visual_embedding:
                fallback = artifacts[definition.fallback_visual_embedding]
                if (
                    fallback.status != CatalogArtifactStatus.REVOKED
                    and available_backends.intersection(fallback.backends)
                ):
                    visual = fallback
                    warnings.append("visual embedder fallback selected for the available backend")
            if visual is None:
                warnings.append(
                    "visual vectors are disabled; caption and page routing remain available"
                )

        selected = [
            (CatalogRole.CHAT, generator),
            (CatalogRole.VL, generator),
            (CatalogRole.EMBEDDING, artifacts[definition.embedding]),
            (CatalogRole.RERANK, artifacts[definition.reranker]),
        ]
        if visual is not None:
            selected.append((CatalogRole.VISUAL_EMBEDDING, visual))

        ollama_models, ollama_available = await self._installed_ollama_digests()
        if not ollama_available:
            warnings.append("Ollama is unavailable; installed model state could not be verified")

        assignments = [
            self._assignment(role, artifact, ollama_models, ollama_available)
            for role, artifact in selected
        ]
        mismatches = [
            item.model
            for item in assignments
            if item.install_state == ModelInstallState.DIGEST_MISMATCH
        ]
        if mismatches:
            warnings.append(
                "installed artifact digest differs from catalog: " + ", ".join(mismatches)
            )

        policy = manifest.performance_profiles[selected_profile]
        context_tokens = min(definition.max_context_tokens, policy.context_ceiling_tokens)
        unique_artifacts = {artifact.id: artifact for _role, artifact in selected}.values()
        runtime_sizes = sorted(
            (artifact.estimated_runtime_bytes for artifact in unique_artifacts), reverse=True
        )
        peak_memory = sum(runtime_sizes[: definition.residency_slots])
        # The visual embedder is catalogued for the generation-pinned media
        # side-index, but V1.1 does not build that index automatically yet.
        # Do not make an unused optional artifact part of readiness or a
        # consented profile download.
        required_assignments = [
            item for item in assignments if item.role != CatalogRole.VISUAL_EMBEDDING
        ]
        downloadable = {
            item.artifact_id: item
            for item in required_assignments
            if item.install_state
            in {
                ModelInstallState.NOT_INSTALLED,
                ModelInstallState.DIGEST_MISMATCH,
            }
        }
        total_download = sum(item.download_bytes for item in downloadable.values())
        ready = bool(required_assignments) and all(
            item.install_state == ModelInstallState.INSTALLED for item in required_assignments
        )
        ready = ready and classification.readiness not in {
            HardwareReadiness.CONSTRAINED,
            HardwareReadiness.UNSUPPORTED,
        }

        stable_payload = "|".join(
            [
                manifest.catalog_id,
                manifest.release,
                selected_profile.value,
                str(tier.value),
                str(context_tokens),
                *(f"{item.role.value}:{item.digest}" for item in assignments),
            ]
        )
        recommendation_id = "rec-" + hashlib.sha256(stable_payload.encode()).hexdigest()[:20]
        stale = datetime.now(UTC).date() > manifest.as_of + timedelta(
            days=manifest.stale_after_days
        )
        if stale:
            warnings.append("the release-bound model catalog is older than 120 days")
        return ModelStackRecommendation(
            recommendation_id=recommendation_id,
            catalog_id=manifest.catalog_id,
            catalog_release=manifest.release,
            catalog_as_of=manifest.as_of,
            catalog_stale=stale,
            profile=selected_profile,
            classification=classification,
            stack_tier=tier,
            assignments=assignments,
            context_tokens=context_tokens,
            residency_slots=definition.residency_slots,
            retrieval_budgets=policy.budgets,
            estimated_peak_memory=peak_memory,
            total_download_bytes=total_download,
            ready_now=ready,
            fallback_tiers=[HardwareTier(value) for value in range(tier.value - 1, 0, -1)],
            warnings=list(dict.fromkeys(warnings)),
        )

    async def profile_preflight(
        self,
        profile: PerformanceProfile | HardwareProfile | str,
        *,
        current_roles: dict[str, str | None] | None = None,
        current_vector_dimension: int | None = None,
        current_embedding_provider: str | None = None,
        current_embedding_digest: str | None = None,
        current_visual_embedding: str | None = None,
        hardware: HardwareInfo | None = None,
        benchmark: HardwareBenchmark | None = None,
        index_has_documents: bool = True,
    ) -> ModelProfilePreflight:
        recommendation = await self.recommend(
            profile,
            hardware=hardware,
            benchmark=benchmark,
        )
        roles = current_roles or {}
        expected: dict[str, str] = {}
        for assignment in recommendation.assignments:
            if assignment.role == CatalogRole.VISUAL_EMBEDDING:
                continue
            expected[assignment.role.value] = assignment.model
        changes = {
            role: model
            for role, model in expected.items()
            if self._normalized_model_name(roles.get(role) or "")
            != self._normalized_model_name(model)
        }
        definition = self._tier_definition(self.curated_catalog(), recommendation.stack_tier)
        embedding_assignment = next(
            item for item in recommendation.assignments if item.role == CatalogRole.EMBEDDING
        )
        old_embedding = roles.get(ModelCategory.EMBEDDING.value)
        requires_reindex = index_has_documents and (
            not old_embedding
            or self._normalized_model_name(old_embedding)
            != self._normalized_model_name(expected[ModelCategory.EMBEDDING.value])
            or (current_embedding_provider or "").casefold() != embedding_assignment.provider.value
            or not current_embedding_digest
            or self._normalized_digest(current_embedding_digest)
            != self._normalized_digest(embedding_assignment.digest)
            or current_vector_dimension != definition.embedding_dimension
        )
        visual = next(
            (
                assignment.model
                for assignment in recommendation.assignments
                if assignment.role == CatalogRole.VISUAL_EMBEDDING
            ),
            None,
        )
        requires_visual_reindex = bool(
            current_visual_embedding
            and self._normalized_model_name(current_visual_embedding)
            != self._normalized_model_name(visual or "")
        )
        downloads = [
            assignment
            for assignment in recommendation.assignments
            if assignment.role != CatalogRole.VISUAL_EMBEDDING
            if assignment.install_state
            in {ModelInstallState.NOT_INSTALLED, ModelInstallState.DIGEST_MISMATCH}
        ]
        warnings = list(recommendation.warnings)
        if downloads:
            warnings.append("models remain unchanged until DOWNLOAD_MODELS consent is supplied")
        if any(item.provider == CatalogProvider.OLLAMA for item in downloads):
            warnings.append(
                "Ollama owns its model filesystem; final free-space validation is performed "
                "by the local daemon during the consented download"
            )
        if any(
            assignment.role != CatalogRole.VISUAL_EMBEDDING
            and assignment.install_state == ModelInstallState.UNKNOWN
            for assignment in recommendation.assignments
        ):
            warnings.append(
                "installed model digests could not be verified; the profile cannot be applied"
            )
        if requires_reindex:
            warnings.append("text embedding change requires explicit REINDEX consent")
        if requires_visual_reindex:
            warnings.append("visual embedding change rebuilds only the media vector index")
        if any(
            assignment.role == CatalogRole.VISUAL_EMBEDDING
            for assignment in recommendation.assignments
        ):
            warnings.append(
                "the optional visual embedder is not downloaded until media-vector indexing "
                "is enabled"
            )
        return ModelProfilePreflight(
            recommendation=recommendation,
            changes=changes,
            downloads=downloads,
            requires_reindex=requires_reindex,
            requires_visual_reindex=requires_visual_reindex,
            can_apply=not any(
                assignment.role != CatalogRole.VISUAL_EMBEDDING
                and assignment.install_state == ModelInstallState.UNKNOWN
                for assignment in recommendation.assignments
            ),
            warnings=list(dict.fromkeys(warnings)),
        )

    async def benchmark(
        self,
        profile: PerformanceProfile | HardwareProfile | str = PerformanceProfile.NORMAL,
        *,
        tier: HardwareTier | int | None = None,
        hardware: HardwareInfo | None = None,
    ) -> HardwareBenchmark:
        """Run a fixed local Ollama canary against already installed artifacts.

        This method never calls ``/api/pull``. The API boundary must require the
        ``BENCHMARK`` confirmation before invoking it because the probe briefly
        loads models that were not resident before the call.
        """

        selected_profile = self.performance_profile(profile)
        snapshot = hardware or self.hardware(self.settings.data_dir)
        recommendation = await self.recommend(
            selected_profile,
            hardware=snapshot,
            tier_override=tier,
            use_cached_benchmark=False,
        )
        assignments = {assignment.role: assignment for assignment in recommendation.assignments}
        required = [CatalogRole.CHAT, CatalogRole.EMBEDDING]
        unavailable = [
            assignments[role].model
            for role in required
            if assignments[role].install_state != ModelInstallState.INSTALLED
        ]
        if unavailable:
            raise ConflictError(
                "Canary requires the exact pinned generator and embedder to be installed; "
                "no models were pulled: " + ", ".join(unavailable)
            )

        before = await self._ollama_json("GET", "/api/ps")
        loaded_before = {
            self._normalized_model_name(str(item.get("name") or item.get("model") or ""))
            for item in before.get("models", [])
        }
        before_vram = sum(int(item.get("size_vram") or 0) for item in before.get("models", []))
        generator = assignments[CatalogRole.CHAT]
        embedder = assignments[CatalogRole.EMBEDDING]
        definition = self._tier_definition(self.curated_catalog(), recommendation.stack_tier)
        # Common one-token word across the supported tokenizer families. The
        # returned prompt_eval_count remains the authoritative measurement.
        prompt = " ".join(["book"] * 2048)
        embed_batch = [
            f"Technisches Fachbuch, Abschnitt {index}: Beleg und Definition." for index in range(32)
        ]
        issues: list[str] = []
        generate_started = time.perf_counter()
        generation: dict[str, Any] = {}
        embedding: dict[str, Any] = {}
        after_vram = before_vram
        try:
            generation = await self._ollama_json(
                "POST",
                "/api/generate",
                {
                    "model": generator.model,
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": "30s",
                    "options": {
                        "num_ctx": max(4096, recommendation.context_tokens),
                        "num_predict": 128,
                        "temperature": 0,
                        "seed": 42,
                    },
                },
                timeout_seconds=180,
            )
            generate_wall = max(time.perf_counter() - generate_started, 0.001)
            embedding_started = time.perf_counter()
            embedding = await self._ollama_json(
                "POST",
                "/api/embed",
                {
                    "model": embedder.model,
                    "input": embed_batch,
                    "dimensions": definition.embedding_dimension,
                    "keep_alive": "30s",
                },
                timeout_seconds=180,
            )
            embedding_wall = max(time.perf_counter() - embedding_started, 0.001)
            after = await self._ollama_json("GET", "/api/ps")
            after_vram = sum(int(item.get("size_vram") or 0) for item in after.get("models", []))
        finally:
            if self._normalized_model_name(generator.model) not in loaded_before:
                try:
                    await self.unload(generator.model)
                except Exception as exc:  # cleanup must not hide valid benchmark output
                    issues.append(f"generator cleanup failed: {exc}")
            if self._normalized_model_name(embedder.model) not in loaded_before:
                try:
                    await self._ollama_json(
                        "POST",
                        "/api/embed",
                        {"model": embedder.model, "input": "cleanup", "keep_alive": 0},
                    )
                except Exception as exc:  # cleanup must not hide valid benchmark output
                    issues.append(f"embedder cleanup failed: {exc}")

        output_tokens = int(generation.get("eval_count") or 0)
        prompt_tokens = int(generation.get("prompt_eval_count") or 0)
        eval_seconds = float(generation.get("eval_duration") or 0) / 1e9
        tokens_per_second = (
            output_tokens / eval_seconds
            if output_tokens and eval_seconds > 0
            else output_tokens / generate_wall
        )
        ttft_ms = (
            float(generation.get("load_duration") or 0)
            + float(generation.get("prompt_eval_duration") or 0)
        ) / 1e6
        if ttft_ms <= 0:
            ttft_ms = generate_wall * 1000
        total_embed_seconds = float(embedding.get("total_duration") or 0) / 1e9
        if total_embed_seconds <= 0:
            total_embed_seconds = embedding_wall
        embedding_items_per_second = len(embed_batch) / total_embed_seconds
        thresholds = {
            PerformanceProfile.FAST: (8.0, 5000.0, 12.0),
            PerformanceProfile.NORMAL: (5.0, 9000.0, 8.0),
            PerformanceProfile.QUALITY: (3.0, 15000.0, 4.0),
        }
        min_tps, max_ttft_ms, min_embedding_rate = thresholds[selected_profile]
        passed = bool(
            output_tokens >= 96
            and tokens_per_second >= min_tps
            and ttft_ms <= max_ttft_ms
            and embedding_items_per_second >= min_embedding_rate
        )
        performance_tier = recommendation.stack_tier
        if not passed:
            performance_tier = HardwareTier(max(1, performance_tier.value - 1))
            issues.append("canary missed the selected profile latency floor; fallback tier advised")
        result = HardwareBenchmark(
            tested_tier=recommendation.stack_tier,
            performance_tier=performance_tier,
            stack_id=recommendation.recommendation_id,
            model_digests={role.value: assignments[role].digest for role in required},
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            embedding_items=len(embed_batch),
            time_to_first_token_ms=ttft_ms,
            tokens_per_second=tokens_per_second,
            embedding_items_per_second=embedding_items_per_second,
            peak_vram_bytes=max(before_vram, after_vram),
            passed=passed,
            not_measured=["rerank", "visual-embedding"],
            issues=issues,
        )
        self._save_cached_benchmark(
            snapshot,
            selected_profile,
            result,
            catalog_release=self.curated_catalog().release,
        )
        return result

    def recommendation_view(
        self,
        recommendation: ModelStackRecommendation,
        *,
        scanned_at: datetime | None = None,
        expert_mode: bool = False,
    ) -> HardwareProfileView:
        manifest = self.curated_catalog()
        definition = self._tier_definition(manifest, recommendation.stack_tier)
        artifacts = {artifact.id: artifact for artifact in manifest.artifacts}
        compact: list[SimpleModelRecommendation] = []
        for assignment in recommendation.assignments:
            artifact = artifacts[assignment.artifact_id]
            reason = {
                CatalogRole.CHAT: "answer model matched to the measured consumer tier",
                CatalogRole.VL: "shared with chat to avoid another resident model",
                CatalogRole.EMBEDDING: "pinned multilingual textbook retrieval model",
                CatalogRole.RERANK: "fast German-capable CPU cross-encoder",
                CatalogRole.VISUAL_EMBEDDING: "separate image index; text embeddings stay stable",
            }[assignment.role]
            compact.append(
                SimpleModelRecommendation(
                    role=assignment.role.value,
                    model=assignment.model,
                    reason=reason,
                    required_bytes=artifact.estimated_runtime_bytes,
                    context_tokens=(
                        recommendation.context_tokens
                        if assignment.role in {CatalogRole.CHAT, CatalogRole.VL}
                        else artifact.context_tokens
                    ),
                )
            )
        limiting_factor = (
            recommendation.classification.limiting_factors[0]
            if recommendation.classification.limiting_factors
            else "balanced capacity"
        )
        return HardwareProfileView(
            tier=recommendation.stack_tier,
            tier_label=definition.label,
            limiting_factor=limiting_factor,
            catalog_version=manifest.release,
            scanned_at=scanned_at or datetime.now(UTC),
            profile=recommendation.profile,
            expert_mode=expert_mode,
            recommendations=compact,
        )

    @staticmethod
    def _tier_definition(manifest: ModelCatalogManifest, tier: HardwareTier) -> ModelTierDefinition:
        return next(definition for definition in manifest.tiers if definition.tier == tier)

    @staticmethod
    def _safe_fallback_generator(
        definition: ModelTierDefinition,
        artifacts: dict[str, ModelArtifact],
    ) -> ModelArtifact:
        if not definition.fallback_generator:
            raise RuntimeError(f"tier {definition.tier.value} has no safe generator fallback")
        fallback = artifacts[definition.fallback_generator]
        if fallback.status == CatalogArtifactStatus.REVOKED:
            raise RuntimeError(f"tier {definition.tier.value} generator and fallback are revoked")
        return fallback

    async def _installed_ollama_digests(self) -> tuple[dict[str, str], bool]:
        try:
            payload = await self._ollama_json("GET", "/api/tags")
        except UpstreamUnavailableError:
            return {}, False
        installed: dict[str, str] = {}
        for raw in payload.get("models", []):
            name = str(raw.get("name") or raw.get("model") or "")
            if name:
                installed[self._normalized_model_name(name)] = str(raw.get("digest") or "")
        return installed, True

    def _assignment(
        self,
        role: CatalogRole,
        artifact: ModelArtifact,
        ollama_models: dict[str, str],
        ollama_available: bool,
    ) -> ModelAssignment:
        installed_digest: str | None = None
        if artifact.provider == CatalogProvider.OLLAMA:
            installed_digest = ollama_models.get(self._normalized_model_name(artifact.model))
            if not ollama_available:
                state = ModelInstallState.UNKNOWN
            elif installed_digest is None:
                state = ModelInstallState.NOT_INSTALLED
            elif hmac.compare_digest(
                self._normalized_digest(installed_digest),
                self._normalized_digest(artifact.digest),
            ):
                state = ModelInstallState.INSTALLED
            else:
                state = ModelInstallState.DIGEST_MISMATCH
        else:
            state = (
                ModelInstallState.INSTALLED
                if self._hugging_face_revision_present(artifact.model, artifact.revision)
                else ModelInstallState.NOT_INSTALLED
            )
            installed_digest = artifact.revision if state == ModelInstallState.INSTALLED else None
        return ModelAssignment(
            role=role,
            artifact_id=artifact.id,
            provider=artifact.provider,
            model=artifact.model,
            revision=artifact.revision,
            digest=artifact.digest,
            quantization=artifact.quantization,
            install_state=state,
            installed_digest=installed_digest,
            download_bytes=artifact.download_bytes,
        )

    @staticmethod
    def hugging_face_cache_root() -> Path:
        """Resolve the same hub cache used by current huggingface_hub."""

        try:
            from huggingface_hub.constants import HF_HUB_CACHE

            return Path(HF_HUB_CACHE).expanduser()
        except ImportError:
            explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
            if explicit:
                return Path(explicit).expanduser()
            hf_home = os.environ.get("HF_HOME")
            if hf_home:
                return Path(hf_home).expanduser() / "hub"
            xdg_cache = os.environ.get("XDG_CACHE_HOME")
            base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
            return base / "huggingface" / "hub"

    @classmethod
    def _hugging_face_revision_present(cls, model: str, revision: str) -> bool:
        hub = cls.hugging_face_cache_root()
        repo = "models--" + model.replace("/", "--")
        snapshot = hub / repo / "snapshots" / revision
        if not (snapshot / "config.json").is_file():
            return False
        # huggingface_hub creates the revision directory before large files
        # finish.  Treating that directory alone as installed made interrupted
        # preset downloads look successful.  A usable transformer snapshot
        # must contain at least one completed weights file.
        weight_names = {
            "model.safetensors",
            "pytorch_model.bin",
            "model.onnx",
            "model.fp16.onnx",
        }
        return any(
            candidate.is_file() and candidate.stat().st_size > 0 and candidate.name in weight_names
            for candidate in snapshot.rglob("*")
        )

    @staticmethod
    def _normalized_model_name(model: str) -> str:
        normalized = model.strip().casefold()
        return normalized.removesuffix(":latest")

    @staticmethod
    def _normalized_digest(digest: str) -> str:
        return digest.strip().casefold().removeprefix("sha256:")

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

    async def install_assignments(
        self,
        assignments: list[ModelAssignment],
    ) -> list[str]:
        """Install explicitly consented, revision-pinned catalog artifacts.

        This is deliberately separate from recommendation and preflight. It is
        never called by startup, scan, benchmark or query code.
        """

        installed: list[str] = []
        unique = {assignment.artifact_id: assignment for assignment in assignments}
        for assignment in unique.values():
            if assignment.install_state == ModelInstallState.INSTALLED:
                continue
            if assignment.provider == CatalogProvider.OLLAMA:
                error: str | None = None
                async for line in self.pull(assignment.model):
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if payload.get("error"):
                        error = str(payload["error"])
                if error:
                    raise UpstreamUnavailableError(
                        f"Ollama could not install {assignment.model}: {error}"
                    )
                digests, available = await self._installed_ollama_digests()
                actual = digests.get(self._normalized_model_name(assignment.model))
                if (
                    not available
                    or actual is None
                    or self._normalized_digest(actual) != self._normalized_digest(assignment.digest)
                ):
                    raise ConflictError(
                        f"Downloaded model {assignment.model} does not match the release catalog",
                        details={
                            "expected_digest": assignment.digest,
                            "actual_digest": actual,
                        },
                    )
            else:
                try:
                    from huggingface_hub import snapshot_download
                except ImportError as exc:
                    raise ConflictError(
                        "Hugging Face model downloads require the installed Haiku model extra"
                    ) from exc
                download_options: dict[str, object] = {}
                if assignment.role == CatalogRole.RERANK:
                    # Cross-encoder repositories often publish the same weights
                    # again as PyTorch, ONNX and OpenVINO variants.  Haiku uses
                    # Transformers/Safetensors, so fetching every export wastes
                    # several gigabytes and makes the advertised package size
                    # incorrect.
                    download_options["allow_patterns"] = [
                        "config.json",
                        "model.safetensors",
                        "sentencepiece.bpe.model",
                        "special_tokens_map.json",
                        "tokenizer.json",
                        "tokenizer_config.json",
                    ]
                await asyncio.to_thread(
                    snapshot_download,
                    repo_id=assignment.model,
                    revision=assignment.revision,
                    cache_dir=str(self.hugging_face_cache_root()),
                    library_name="omarag",
                    **download_options,
                )
                if not self._hugging_face_revision_present(assignment.model, assignment.revision):
                    raise ConflictError(
                        f"Pinned Hugging Face revision was not cached: {assignment.model}",
                        details={"revision": assignment.revision},
                    )
            installed.append(assignment.model)
        return installed

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
                    artifact_digest=str(raw.get("digest") or "") or None,
                    artifact_revision=model_id.partition(":")[2] or "latest",
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
    def hardware(
        data_path: Path | None = None,
        *,
        proc_root: Path = Path("/proc"),
        sys_root: Path = Path("/sys"),
    ) -> HardwareInfo:
        """Discover stable capacity and transient readiness without mutations."""

        warnings: list[str] = []
        memory = ModelService._read_meminfo(proc_root / "meminfo")
        cpu_model, cpu_features, physical_cores = ModelService._read_cpuinfo(proc_root / "cpuinfo")
        accelerators = ModelService._scan_nvidia()
        nvidia_seen = bool(accelerators)
        accelerators.extend(
            ModelService._scan_drm(sys_root, skip_nvidia=nvidia_seen, warnings=warnings)
        )
        accelerators.sort(
            key=lambda item: (item.dedicated_memory_total, item.name.casefold()), reverse=True
        )
        primary = accelerators[0] if accelerators else None
        storage_total = 0
        storage_available = 0
        storage_path = (data_path or Path("/")).expanduser()
        while not storage_path.exists() and storage_path != storage_path.parent:
            storage_path = storage_path.parent
        try:
            disk = shutil.disk_usage(storage_path)
            storage_total = disk.total
            storage_available = disk.free
        except OSError as exc:
            warnings.append(f"storage scan failed: {exc}")

        system = platform.system().casefold() or "unknown"
        architecture = platform.machine().casefold() or "unknown"
        memory_total = memory.get("MemTotal", 0)
        memory_available = memory.get("MemAvailable", 0)
        vram_total = primary.dedicated_memory_total if primary else 0
        vram_used = primary.dedicated_memory_used if primary else 0
        integrated_reserved = max(
            (
                accelerator.dedicated_memory_total
                for accelerator in accelerators
                if accelerator.integrated
            ),
            default=0,
        )
        discrete = [accelerator for accelerator in accelerators if not accelerator.integrated]
        dedicated_vram_total = max(
            (accelerator.dedicated_memory_total for accelerator in discrete), default=0
        )
        dedicated_vram_used = max(
            (accelerator.dedicated_memory_used for accelerator in discrete), default=0
        )
        shared_memory = max(
            (accelerator.shared_memory_total for accelerator in accelerators), default=0
        )
        provisional = HardwareInfo(
            platform=system,
            architecture=architecture,
            cpu_model=cpu_model,
            logical_cores=os.cpu_count() or 0,
            physical_cores=physical_cores,
            cpu_features=cpu_features,
            memory_total=memory_total,
            memory_capacity=memory_total + integrated_reserved,
            memory_available=memory_available,
            gpu=primary.name if primary else "No dedicated GPU detected",
            vram_total=vram_total,
            vram_used=vram_used,
            shared_memory=shared_memory,
            dedicated_vram_total=dedicated_vram_total,
            dedicated_vram_used=dedicated_vram_used,
            accelerators=accelerators,
            storage_total=storage_total,
            storage_available=storage_available,
            scan_warnings=warnings,
        )
        classification = ModelService.classify_hardware(provisional)
        return provisional.model_copy(
            update={
                "capacity_tier": classification.capacity_tier,
                "readiness_tier": classification.readiness_tier,
                "readiness": classification.readiness,
                "limiting_factors": classification.limiting_factors,
            }
        )

    @staticmethod
    def _tier_from_resources(memory_bytes: int, vram_bytes: int) -> HardwareTier:
        """Classify consumer hardware; shared/UMA memory is never double-counted."""

        ram = memory_bytes / GIB
        vram = vram_bytes / GIB
        if ram >= 60 and vram >= 22:
            return HardwareTier.TIER_10
        if (ram >= 30 and vram >= 22) or (ram >= 46 and vram >= 18) or (ram >= 60 and vram >= 14):
            return HardwareTier.TIER_9
        if (ram >= 30 and vram >= 14) or (ram >= 46 and vram >= 10):
            return HardwareTier.TIER_8
        if ram >= 30 and vram >= 7:
            return HardwareTier.TIER_7
        if (ram >= 22 and vram >= 7) or ram >= 30:
            return HardwareTier.TIER_6
        if ram >= 14 and vram >= 7:
            return HardwareTier.TIER_5
        if ram >= 14 and vram >= 3.5:
            return HardwareTier.TIER_4
        if ram >= 14:
            return HardwareTier.TIER_3
        if ram >= 10 or (ram >= 7 and vram >= 3.5):
            return HardwareTier.TIER_2
        return HardwareTier.TIER_1

    @staticmethod
    def _read_meminfo(path: Path) -> dict[str, int]:
        values: dict[str, int] = {}
        try:
            for line in path.read_text().splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return {}
        return values

    @staticmethod
    def _read_cpuinfo(path: Path) -> tuple[str, list[str], int | None]:
        try:
            text = path.read_text()
        except OSError:
            return "Unknown CPU", [], None
        model = "Unknown CPU"
        features: set[str] = set()
        physical_pairs: set[tuple[str, str]] = set()
        for block in text.split("\n\n"):
            values: dict[str, str] = {}
            for line in block.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                values[key.strip()] = value.strip()
            model = values.get("model name") or values.get("Processor") or model
            features.update((values.get("flags") or values.get("Features") or "").split())
            if "physical id" in values and "core id" in values:
                physical_pairs.add((values["physical id"], values["core id"]))
        useful = sorted(
            features.intersection(
                {
                    "avx",
                    "avx2",
                    "avx512f",
                    "f16c",
                    "fma",
                    "sse4_1",
                    "sse4_2",
                    "vnni",
                }
            )
        )
        return model, useful, len(physical_pairs) or None

    @staticmethod
    def _scan_drm(
        sys_root: Path,
        *,
        skip_nvidia: bool,
        warnings: list[str],
    ) -> list[AcceleratorInfo]:
        vendor_names = {
            "0x1002": "AMD",
            "0x10de": "NVIDIA",
            "0x8086": "Intel",
        }
        friendly_devices = {
            ("0x1002", "0x1900"): "AMD Radeon 760M",
        }
        result: list[AcceleratorInfo] = []
        drm = sys_root / "class" / "drm"
        try:
            cards = sorted(drm.glob("card[0-9]*"))
        except OSError as exc:
            warnings.append(f"DRM scan failed: {exc}")
            return result
        for card in cards:
            if not re.fullmatch(r"card\d+", card.name):
                continue
            device = card / "device"
            vendor_id = ModelService._read_text(device / "vendor")
            if skip_nvidia and vendor_id == "0x10de":
                continue
            device_id = ModelService._read_text(device / "device") or None
            vendor = vendor_names.get(vendor_id, vendor_id or "unknown")
            total = ModelService._read_int(device / "mem_info_vram_total")
            used = ModelService._read_int(device / "mem_info_vram_used")
            shared = ModelService._read_int(device / "mem_info_gtt_total")
            integrated = total <= 2 * GIB and bool(shared or vendor == "Intel")
            backends = ["vulkan"]
            if vendor == "AMD" and Path("/dev/kfd").exists():
                backends.insert(0, "rocm")
            elif vendor == "NVIDIA":
                backends.insert(0, "cuda")
            name = friendly_devices.get(
                (vendor_id, device_id or ""),
                f"{vendor} GPU {device_id or card.name}",
            )
            driver = None
            with suppress(OSError):
                driver = (device / "driver").resolve(strict=True).name
            result.append(
                AcceleratorInfo(
                    id=card.name,
                    name=name,
                    vendor=vendor,
                    device_id=device_id,
                    driver=driver,
                    backends=backends,
                    dedicated_memory_total=total,
                    dedicated_memory_used=min(used, total) if total else 0,
                    shared_memory_total=shared,
                    integrated=integrated,
                )
            )
        return result

    @staticmethod
    def _scan_nvidia() -> list[AcceleratorInfo]:
        command = [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,memory.used,driver_version",
            "--format=csv,noheader,nounits",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0:
            return []
        result: list[AcceleratorInfo] = []
        for line in completed.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 5)]
            if len(parts) != 6:
                continue
            index, uuid, name, total_mib, used_mib, driver = parts
            try:
                total = int(float(total_mib)) * MIB
                used = int(float(used_mib)) * MIB
            except ValueError:
                continue
            result.append(
                AcceleratorInfo(
                    id=uuid or f"nvidia-{index}",
                    name=name or f"NVIDIA GPU {index}",
                    vendor="NVIDIA",
                    driver=driver or None,
                    backends=["cuda", "vulkan"],
                    dedicated_memory_total=total,
                    dedicated_memory_used=min(used, total),
                    integrated=False,
                )
            )
        return result

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
        capacity_ram, capacity_vram, available_ram, available_vram = (
            ModelService._classification_resources(hardware)
        )
        ram_usable = min(available_ram, int(capacity_ram * 0.80)) - 2 * GIB
        vram_usable = min(available_vram, int(capacity_vram * 0.85)) - 768 * MIB
        # Dedicated VRAM can hold offloaded layers while RAM holds the remainder.
        # Shared/UMA memory is intentionally excluded because it is already RAM.
        usable = max(ram_usable, vram_usable, ram_usable + max(vram_usable, 0))
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
                        f"qwen3.5:{qwen_size[0]}-q4_K_M",
                        ModelSource.OLLAMA,
                        qwen_size[1],
                    ),
                    (
                        ModelCategory.VL,
                        f"qwen3.5:{qwen_size[0]}-q4_K_M",
                        ModelSource.OLLAMA,
                        qwen_size[1],
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
                "qwen-depth",
                "Balanced",
                "The recommended balance of answer quality, speed and memory.",
                "The same Qwen embedding/rerank pair avoids cross-family retrieval drift.",
                [
                    (
                        ModelCategory.CHAT,
                        f"qwen3.5:{deeper_qwen_size[0]}-q4_K_M",
                        ModelSource.OLLAMA,
                        deeper_qwen_size[1],
                    ),
                    (
                        ModelCategory.VL,
                        f"qwen3.5:{deeper_qwen_size[0]}-q4_K_M",
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
                    (
                        ModelCategory.CHAT,
                        "qwen3.5:4b-q4_K_M",
                        ModelSource.OLLAMA,
                        4_000_000_000,
                    ),
                    (
                        ModelCategory.VL,
                        "qwen3.5:4b-q4_K_M",
                        ModelSource.OLLAMA,
                        4_000_000_000,
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
                        568_000_000,
                    ),
                ],
            ),
        ]
        packages: list[ModelPackage] = []
        pinned_hugging_face = {
            artifact.model: artifact
            for artifact in self.curated_catalog().artifacts
            if artifact.provider == CatalogProvider.HUGGING_FACE
        }
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
                if source == ModelSource.HUGGING_FACE:
                    artifact = pinned_hugging_face.get(model)
                    model_installed = bool(
                        artifact and self._hugging_face_revision_present(model, artifact.revision)
                    )
                else:
                    model_installed = any(
                        installed == model
                        or installed.startswith(f"{model}-")
                        or model.casefold() in installed.casefold()
                        for installed in installed_names
                    )
                items.append(
                    ModelPackageItem(
                        role=role,
                        model=model,
                        download_name=download_name,
                        source=source,
                        installed=model_installed,
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
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
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
