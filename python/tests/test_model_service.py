from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

import pytest
from pydantic import ValidationError

from omarag_bridge.models.api import (
    HardwareBenchmarkRequest,
    ModelProfileApplyRequest,
    ModelRecommendationRequest,
    RunOptions,
)
from omarag_bridge.models.domain import (
    AcceleratorInfo,
    CatalogRole,
    HardwareBenchmark,
    HardwareInfo,
    HardwareProfile,
    HardwareReadiness,
    HardwareTier,
    ModelCatalogEntry,
    ModelCategory,
    ModelFit,
    ModelInstallState,
    ModelResidency,
    ModelSource,
    PerformanceProfile,
)
from omarag_bridge.services.model_service import ModelService


def entry(parameters: int, category: ModelCategory = ModelCategory.CHAT) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id="test/model",
        source=ModelSource.HUGGING_FACE,
        category=category,
        parameter_count=parameters,
        estimated_memory=0,
        fit=ModelFit.TIGHT,
    )


def test_hardware_filter_rejects_models_that_would_exhaust_memory() -> None:
    hardware = HardwareInfo(memory_total=14 * 1024**3, memory_available=8 * 1024**3)
    small = ModelService.estimated_memory(entry(2_000_000_000), "Q4_K_M", 8192)
    large = ModelService.estimated_memory(entry(14_000_000_000), "Q4_K_M", 8192)
    assert ModelService.fit(small, hardware) == ModelFit.COMFORTABLE
    assert ModelService.fit(large, hardware) is None


def test_hardware_benchmark_cache_is_device_profile_and_catalog_bound(
    tmp_path: Path,
) -> None:
    service = ModelService.__new__(ModelService)
    service.settings = type("Settings", (), {"data_dir": tmp_path})()
    hardware = HardwareInfo(
        cpu_model="Test CPU",
        logical_cores=8,
        memory_total=16 * 1024**3,
        memory_capacity=16 * 1024**3,
        memory_available=12 * 1024**3,
    )
    manifest = service.curated_catalog()
    definition = next(item for item in manifest.tiers if item.tier == HardwareTier.TIER_3)
    artifacts = {item.id: item for item in manifest.artifacts}
    result = HardwareBenchmark(
        tested_tier=HardwareTier.TIER_3,
        performance_tier=HardwareTier.TIER_2,
        stack_id="rec-test",
        model_digests={
            "chat": artifacts[definition.generator].digest,
            "embedding": artifacts[definition.embedding].digest,
        },
    )
    service._save_cached_benchmark(
        hardware,
        PerformanceProfile.NORMAL,
        result,
        catalog_release="2026.08.1",
    )

    assert (
        service._load_cached_benchmark(
            hardware,
            PerformanceProfile.NORMAL,
            catalog_release="2026.08.1",
        )
        == result
    )
    assert (
        service._load_cached_benchmark(
            hardware,
            PerformanceProfile.FAST,
            catalog_release="2026.08.1",
        )
        is None
    )
    service._save_cached_benchmark(
        hardware,
        PerformanceProfile.FAST,
        result,
        catalog_release="2026.08.1",
    )
    assert (
        service._load_cached_benchmark(
            hardware,
            PerformanceProfile.NORMAL,
            catalog_release="2026.08.1",
        )
        == result
    )
    assert (
        service._load_cached_benchmark(
            hardware,
            PerformanceProfile.FAST,
            catalog_release="2026.08.1",
        )
        == result
    )
    changed = hardware.model_copy(update={"memory_capacity": 32 * 1024**3})
    assert (
        service._load_cached_benchmark(
            changed,
            PerformanceProfile.NORMAL,
            catalog_release="2026.08.1",
        )
        is None
    )


def test_model_roles_cover_chat_vl_embedding_and_rerank() -> None:
    assert ModelService._category("qwen3", [], "") == ModelCategory.CHAT
    assert ModelService._category("qwen3-vl", [], "") == ModelCategory.VL
    assert ModelService._category("qwen3.5:4b", [], "") == ModelCategory.VL
    assert ModelService._category("nomic-embed-text", [], "") == ModelCategory.EMBEDDING
    assert ModelService._category("bge-m3", [], "") == ModelCategory.EMBEDDING
    assert ModelService._category("bge-reranker", [], "") == ModelCategory.RERANK
    assert (
        ModelService._category("acme/embedding-model", ["feature-extraction", "reranker"], "")
        == ModelCategory.EMBEDDING
    )


def test_three_recommendations_are_ranked_for_the_selected_profile() -> None:
    entries = [entry(size * 1_000_000_000) for size in range(1, 6)]
    for item in entries:
        item.fit = ModelFit.COMFORTABLE
        item.downloads = 1000
    ModelService._rank_recommendations(entries, ModelCategory.CHAT, HardwareProfile.LAPTOP)
    ranks = sorted(item.recommended_rank for item in entries if item.recommended_rank)
    assert ranks == [1, 2, 3]


def test_recommendations_prefer_trusted_base_models_over_uncensored_tunes() -> None:
    trusted = entry(2_000_000_000)
    trusted.id = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
    trusted.downloads = 100
    trusted.likes = 10
    trusted.fit = ModelFit.COMFORTABLE
    unsafe = entry(2_000_000_000)
    unsafe.id = "example/Qwen-Uncensored-GGUF"
    unsafe.downloads = 100_000_000
    unsafe.likes = 100_000
    unsafe.fit = ModelFit.COMFORTABLE
    fillers = [entry(size * 1_000_000_000) for size in (1, 3, 4)]
    for index, item in enumerate(fillers):
        item.id = f"trusted/model-{index}"
        item.fit = ModelFit.COMFORTABLE
    entries = [trusted, unsafe, *fillers]
    ModelService._rank_recommendations(entries, ModelCategory.CHAT, HardwareProfile.LAPTOP)
    assert trusted.recommended_rank is not None
    assert unsafe.recommended_rank is None


def test_three_hardware_fitting_packages_preserve_retrieval_family_synergy() -> None:
    service = ModelService.__new__(ModelService)
    hardware = HardwareInfo(memory_total=14 * 1024**3, memory_available=8 * 1024**3)
    packages = service._recommended_packages(
        hardware,
        set(),
        "Q4_K_M",
        8192,
        HardwareProfile.LAPTOP,
    )
    assert [package.recommended_rank for package in packages] == [1, 2, 3]
    assert all(package.fit == ModelFit.COMFORTABLE for package in packages)
    assert {item.role for item in packages[0].models} == set(ModelCategory)
    assert "Qwen" in packages[0].synergy
    assert "cross-encoder" in packages[2].synergy


@pytest.mark.asyncio
async def test_runtime_maps_each_configured_role_to_its_residency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)

    async def ollama_json(*_args, **_kwargs):
        return {
            "models": [
                {
                    "name": "qwen:latest",
                    "size": 123,
                    "details": {"parameter_size": "2B", "quantization_level": "Q4"},
                }
            ]
        }

    monkeypatch.setattr(service, "_ollama_json", ollama_json)
    runtime = await service.runtime(
        {
            "chat": "qwen",
            "vl": "qwen",
            "embedding": "embed",
            "rerank": None,
        },
        active_roles={ModelCategory.CHAT},
        worker_timeout_seconds=45.0,
    )
    roles = {role.role.value: role for role in runtime.roles}
    assert roles["chat"].residency == ModelResidency.ACTIVE
    assert roles["chat"].shared_with == [ModelCategory.VL]
    assert roles["embedding"].residency == ModelResidency.IDLE
    assert roles["rerank"].residency == ModelResidency.UNCONFIGURED
    assert runtime.query_worker_state == "active"


@pytest.mark.parametrize(
    ("ram_gib", "vram_gib", "expected"),
    [
        (8, 0, HardwareTier.TIER_1),
        (12, 0, HardwareTier.TIER_2),
        (16, 0, HardwareTier.TIER_3),
        (16, 4, HardwareTier.TIER_4),
        (16, 8, HardwareTier.TIER_5),
        (24, 8, HardwareTier.TIER_6),
        (32, 8, HardwareTier.TIER_7),
        (32, 16, HardwareTier.TIER_8),
        (48, 20, HardwareTier.TIER_9),
        (64, 24, HardwareTier.TIER_10),
    ],
)
def test_consumer_hardware_tiers_are_deterministic(
    ram_gib: int,
    vram_gib: int,
    expected: HardwareTier,
) -> None:
    assert ModelService._tier_from_resources(ram_gib * 1024**3, vram_gib * 1024**3) == expected


def test_capacity_is_stable_while_readiness_reflects_current_pressure() -> None:
    hardware = HardwareInfo(
        memory_total=32 * 1024**3,
        memory_available=6 * 1024**3,
        vram_total=16 * 1024**3,
        vram_used=15 * 1024**3,
    )
    classification = ModelService.classify_hardware(hardware)
    assert classification.capacity_tier == HardwareTier.TIER_8
    assert classification.readiness_tier == HardwareTier.TIER_1
    assert classification.effective_tier == HardwareTier.TIER_8
    assert classification.readiness == HardwareReadiness.CONSTRAINED


def test_embedded_catalog_has_all_tiers_profiles_and_valid_checksum() -> None:
    package = resources.files("omarag_bridge.catalog")
    raw = package.joinpath("model_catalog_2026_08.json").read_bytes()
    expected = package.joinpath("model_catalog_2026_08.json.sha256").read_text().split()[0]
    assert hashlib.sha256(raw).hexdigest() == expected

    manifest = ModelService.curated_catalog()
    assert [definition.tier.value for definition in manifest.tiers] == list(range(1, 11))
    assert set(manifest.performance_profiles) == set(PerformanceProfile)
    quality = manifest.performance_profiles[PerformanceProfile.QUALITY]
    assert quality.budgets["complex"].max_sources == 18
    assert len({definition.embedding for definition in manifest.tiers}) == 1
    assert all(artifact.revision and artifact.digest for artifact in manifest.artifacts)
    assert all(":latest" not in artifact.model for artifact in manifest.artifacts)


def test_v11_profile_contracts_accept_legacy_names_but_require_mutation_consent() -> None:
    assert HardwareBenchmarkRequest(profile="balanced", confirm="BENCHMARK").profile == "normal"
    assert ModelRecommendationRequest(performance_profile="deep").performance_profile == "quality"
    assert RunOptions(profile="normal").profile == "normal"
    assert RunOptions(profile="balanced").profile == "balanced"
    with pytest.raises(ValidationError):
        ModelProfileApplyRequest.model_validate({"preflight_id": "preflight-1"})


def test_hardware_scan_reports_all_accelerators_without_adding_vram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    device = sys / "class/drm/card0/device"
    proc.mkdir()
    device.mkdir(parents=True)
    (proc / "meminfo").write_text(f"MemTotal: {64 * 1024**2} kB\nMemAvailable: {32 * 1024**2} kB\n")
    (proc / "cpuinfo").write_text(
        "processor: 0\nmodel name: Test CPU\nphysical id: 0\ncore id: 0\nflags: avx2 fma\n"
    )
    (device / "vendor").write_text("0x1002\n")
    (device / "device").write_text("0x9999\n")
    (device / "mem_info_vram_total").write_text(str(8 * 1024**3))
    (device / "mem_info_vram_used").write_text(str(1 * 1024**3))
    (device / "mem_info_gtt_total").write_text(str(16 * 1024**3))
    monkeypatch.setattr(
        ModelService,
        "_scan_nvidia",
        staticmethod(
            lambda: [
                AcceleratorInfo(
                    id="GPU-test",
                    name="NVIDIA Test",
                    vendor="NVIDIA",
                    backends=["cuda"],
                    dedicated_memory_total=24 * 1024**3,
                    dedicated_memory_used=2 * 1024**3,
                )
            ]
        ),
    )

    hardware = ModelService.hardware(tmp_path, proc_root=proc, sys_root=sys)
    assert [item.vendor for item in hardware.accelerators] == ["NVIDIA", "AMD"]
    assert hardware.vram_total == 24 * 1024**3
    assert hardware.shared_memory == 16 * 1024**3
    assert hardware.capacity_tier == HardwareTier.TIER_10


@pytest.mark.asyncio
async def test_curated_recommendation_uses_exact_pins_and_visual_backend_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    service.settings = type("Settings", (), {"data_dir": Path("/")})()
    manifest = service.curated_catalog()
    exact = {
        service._normalized_model_name(artifact.model): artifact.digest.removeprefix("sha256:")
        for artifact in manifest.artifacts
        if artifact.provider.value == "ollama"
    }

    async def installed():
        return exact, True

    monkeypatch.setattr(service, "_installed_ollama_digests", installed)
    monkeypatch.setattr(service, "_hugging_face_revision_present", lambda *_args: True)
    hardware = HardwareInfo(
        memory_total=48 * 1024**3,
        memory_available=40 * 1024**3,
        vram_total=12 * 1024**3,
        vram_used=0,
    )
    recommendation = await service.recommend(PerformanceProfile.QUALITY, hardware=hardware)
    by_role = {assignment.role: assignment for assignment in recommendation.assignments}
    assert recommendation.stack_tier == HardwareTier.TIER_8
    assert recommendation.context_tokens == 24576
    assert by_role[CatalogRole.CHAT].digest.startswith("sha256:")
    assert by_role[CatalogRole.CHAT].install_state == ModelInstallState.INSTALLED
    assert by_role[CatalogRole.VISUAL_EMBEDDING].model == "google/siglip2-base-patch16-224"
    assert recommendation.ready_now is True
    assert recommendation.total_download_bytes == 0


@pytest.mark.asyncio
async def test_profile_preflight_never_changes_models_and_marks_embedding_reindex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    service.settings = type("Settings", (), {"data_dir": Path("/")})()

    async def installed():
        return {}, True

    monkeypatch.setattr(service, "_installed_ollama_digests", installed)
    monkeypatch.setattr(service, "_hugging_face_revision_present", lambda *_args: False)
    hardware = HardwareInfo(memory_total=16 * 1024**3, memory_available=12 * 1024**3)
    result = await service.profile_preflight(
        "balanced",
        current_roles={
            "chat": "old-chat",
            "vl": "old-vl",
            "embedding": "old-embedding",
            "rerank": "old-reranker",
        },
        current_vector_dimension=768,
        hardware=hardware,
    )
    assert result.recommendation.profile == PerformanceProfile.NORMAL
    assert result.requires_reindex is True
    assert result.downloads
    assert any("DOWNLOAD_MODELS" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_optional_visual_embedder_is_not_downloaded_before_media_vectors_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    service.settings = type("Settings", (), {"data_dir": Path("/")})()
    manifest = service.curated_catalog()
    exact = {
        service._normalized_model_name(artifact.model): artifact.digest
        for artifact in manifest.artifacts
        if artifact.provider.value == "ollama"
    }

    async def installed():
        return exact, True

    def hf_present(model: str, _revision: str) -> bool:
        return model.startswith("cross-encoder/")

    monkeypatch.setattr(service, "_installed_ollama_digests", installed)
    monkeypatch.setattr(service, "_hugging_face_revision_present", hf_present)
    hardware = HardwareInfo(
        memory_total=48 * 1024**3,
        memory_available=40 * 1024**3,
        vram_total=12 * 1024**3,
    )
    recommendation = await service.recommend("normal", hardware=hardware)
    current = {
        assignment.role.value: assignment.model
        for assignment in recommendation.assignments
        if assignment.role != CatalogRole.VISUAL_EMBEDDING
    }
    preflight = await service.profile_preflight(
        "normal",
        current_roles=current,
        current_vector_dimension=1024,
        hardware=hardware,
    )

    visual = next(
        item for item in recommendation.assignments if item.role == CatalogRole.VISUAL_EMBEDDING
    )
    assert visual.install_state == ModelInstallState.NOT_INSTALLED
    assert recommendation.ready_now is True
    assert recommendation.total_download_bytes == 0
    assert preflight.downloads == []
    assert preflight.can_apply is True
    assert any("optional visual embedder" in warning for warning in preflight.warnings)


@pytest.mark.asyncio
async def test_profile_cannot_apply_when_ollama_digests_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    service.settings = type("Settings", (), {"data_dir": Path("/")})()

    async def unavailable():
        return {}, False

    monkeypatch.setattr(service, "_installed_ollama_digests", unavailable)
    monkeypatch.setattr(service, "_hugging_face_revision_present", lambda *_args: True)
    result = await service.profile_preflight(
        "normal",
        current_roles={},
        current_vector_dimension=1024,
        hardware=HardwareInfo(memory_total=16 * 1024**3, memory_available=12 * 1024**3),
        index_has_documents=False,
    )

    assert result.downloads == []
    assert result.can_apply is False
    assert any("could not be verified" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_existing_index_compares_the_full_embedding_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    service.settings = type("Settings", (), {"data_dir": Path("/")})()
    manifest = service.curated_catalog()
    exact = {
        service._normalized_model_name(artifact.model): artifact.digest
        for artifact in manifest.artifacts
        if artifact.provider.value == "ollama"
    }

    async def installed():
        return exact, True

    monkeypatch.setattr(service, "_installed_ollama_digests", installed)
    monkeypatch.setattr(service, "_hugging_face_revision_present", lambda *_args: True)
    hardware = HardwareInfo(memory_total=16 * 1024**3, memory_available=12 * 1024**3)
    recommendation = await service.recommend("normal", hardware=hardware)
    current = {
        item.role.value: item.model
        for item in recommendation.assignments
        if item.role != CatalogRole.VISUAL_EMBEDDING
    }
    embedding = next(
        item for item in recommendation.assignments if item.role == CatalogRole.EMBEDDING
    )

    exact_preflight = await service.profile_preflight(
        "normal",
        current_roles=current,
        current_vector_dimension=1024,
        current_embedding_provider="ollama",
        current_embedding_digest=embedding.digest,
        hardware=hardware,
        index_has_documents=True,
    )
    stale_preflight = await service.profile_preflight(
        "normal",
        current_roles=current,
        current_vector_dimension=1024,
        current_embedding_provider="ollama",
        current_embedding_digest="sha256:stale-vector-space",
        hardware=hardware,
        index_has_documents=True,
    )

    assert exact_preflight.requires_reindex is False
    assert stale_preflight.requires_reindex is True


@pytest.mark.asyncio
async def test_profile_can_repair_digest_mismatch_after_download_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    service.settings = type("Settings", (), {"data_dir": Path("/")})()

    async def mismatched():
        manifest = service.curated_catalog()
        return {
            service._normalized_model_name(artifact.model): "sha256:wrong"
            for artifact in manifest.artifacts
            if artifact.provider.value == "ollama"
        }, True

    monkeypatch.setattr(service, "_installed_ollama_digests", mismatched)
    monkeypatch.setattr(service, "_hugging_face_revision_present", lambda *_args: True)
    result = await service.profile_preflight(
        "normal",
        current_roles={},
        current_vector_dimension=1024,
        hardware=HardwareInfo(memory_total=16 * 1024**3, memory_available=12 * 1024**3),
        index_has_documents=False,
    )

    assert result.downloads
    assert result.can_apply is True


@pytest.mark.asyncio
async def test_benchmark_uses_installed_models_and_never_pulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    service.settings = type("Settings", (), {"data_dir": Path("/")})()
    monkeypatch.setattr(
        service,
        "_load_cached_benchmark",
        lambda *_args, **_kwargs: pytest.fail("an explicit canary must ignore cached tiers"),
    )
    manifest = service.curated_catalog()
    exact = {
        service._normalized_model_name(artifact.model): artifact.digest
        for artifact in manifest.artifacts
        if artifact.provider.value == "ollama"
    }
    calls: list[tuple[str, str]] = []

    async def installed():
        return exact, True

    async def ollama_json(method, path, body=None, **_kwargs):
        calls.append((method, path))
        if path == "/api/ps":
            return {"models": []}
        if path == "/api/generate":
            return {
                "prompt_eval_count": 2048,
                "prompt_eval_duration": 2_000_000_000,
                "eval_count": 128,
                "eval_duration": 8_000_000_000,
            }
        if path == "/api/embed":
            return {"embeddings": [[0.0]], "total_duration": 1_000_000_000}
        raise AssertionError(path)

    async def unload(model):
        calls.append(("UNLOAD", model))

    monkeypatch.setattr(service, "_installed_ollama_digests", installed)
    monkeypatch.setattr(service, "_hugging_face_revision_present", lambda *_args: True)
    monkeypatch.setattr(service, "_ollama_json", ollama_json)
    monkeypatch.setattr(service, "unload", unload)
    hardware = HardwareInfo(memory_total=16 * 1024**3, memory_available=12 * 1024**3)
    result = await service.benchmark("normal", tier=HardwareTier.TIER_3, hardware=hardware)
    assert result.passed is True
    assert result.output_tokens == 128
    assert result.not_measured == ["rerank", "visual-embedding"]
    assert all(path != "/api/pull" for _method, path in calls)
