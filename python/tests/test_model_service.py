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
    ModelAssignment,
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


def test_three_hardware_fitting_packages_preserve_retrieval_family_synergy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    monkeypatch.setattr(
        service,
        "_hugging_face_revision_present",
        lambda model, _revision: model.startswith("cross-encoder/"),
    )
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
    assert next(item for item in packages[0].models if item.role == ModelCategory.RERANK).installed
    assert "Qwen" in packages[0].synergy
    assert "cross-encoder" in packages[2].synergy


def test_hugging_face_revision_is_not_installed_until_weights_are_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = "cross-encoder/example"
    revision = "revision-1"
    snapshot = tmp_path / "models--cross-encoder--example" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)

    assert not ModelService._hugging_face_revision_present(model, revision)
    (snapshot / "model.safetensors").write_bytes(b"weights")
    assert ModelService._hugging_face_revision_present(model, revision)


@pytest.mark.asyncio
async def test_reranker_install_downloads_only_transformer_runtime_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelService.__new__(ModelService)
    artifact = next(
        item for item in service.curated_catalog().artifacts if CatalogRole.RERANK in item.roles
    )
    assignment = ModelAssignment(
        role=CatalogRole.RERANK,
        artifact_id=artifact.id,
        provider=artifact.provider,
        model=artifact.model,
        revision=artifact.revision,
        digest=artifact.digest,
        quantization=artifact.quantization,
        install_state=ModelInstallState.NOT_INSTALLED,
        download_bytes=artifact.download_bytes,
    )
    captured: dict[str, object] = {}

    def snapshot_download(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    monkeypatch.setattr(service, "_hugging_face_revision_present", lambda *_: True)

    await service.install_assignments([assignment])

    patterns = captured["allow_patterns"]
    assert isinstance(patterns, list)
    assert "model.safetensors" in patterns
    assert not any("onnx" in pattern for pattern in patterns)
    assert not any("pytorch_model.bin" in pattern for pattern in patterns)


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


def test_pinned_hugging_face_install_stays_loadable_under_its_default_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sha-pinned download must still resolve for consumers asking for "main".

    huggingface_hub only writes ``refs/<name>`` when the requested revision is a
    branch or tag.  Downloading a pinned commit therefore leaves the repository
    reachable exclusively by that commit, and every library that loads the model
    by name -- sentence-transformers, transformers, docling -- fails inside the
    offline worker with "cannot find an appropriate cached snapshot folder".
    """

    model = "cross-encoder/example"
    revision = "1427fd652930e4ba29e8149678df786c240d8825"
    snapshot = tmp_path / "models--cross-encoder--example" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)

    ModelService._link_pinned_revision_to_default(model, revision)

    ref = tmp_path / "models--cross-encoder--example" / "refs" / "main"
    assert ref.read_text() == revision


def test_default_revision_link_never_overwrites_an_existing_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = "cross-encoder/example"
    repo = tmp_path / "models--cross-encoder--example"
    snapshot = repo / "snapshots" / "aaa"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("bbb")
    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)

    ModelService._link_pinned_revision_to_default(model, "aaa")

    assert (repo / "refs" / "main").read_text() == "bbb"


def test_default_revision_link_is_skipped_for_an_incomplete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted download must not be advertised as the default revision."""

    model = "cross-encoder/example"
    repo = tmp_path / "models--cross-encoder--example"
    snapshot = repo / "snapshots" / "aaa"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)

    ModelService._link_pinned_revision_to_default(model, "aaa")

    assert not (repo / "refs").exists()


def _cache_repo(root: Path, model: str, revision: str, *, complete: bool = True) -> Path:
    """Lay out a hub cache entry the way huggingface_hub does."""

    repo = root / ("models--" + model.replace("/", "--"))
    sha = "0" * 40
    snapshot = repo / "snapshots" / sha
    snapshot.mkdir(parents=True)
    if complete:
        (snapshot / "config.json").write_text("{}")
    (repo / "refs").mkdir(parents=True, exist_ok=True)
    (repo / "refs" / revision).write_text(sha)
    return repo


def test_conversion_artifacts_report_names_what_an_import_worker_would_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty cache must be reported as not ready, listing every repository.

    The import worker runs with ``HF_HUB_OFFLINE=1``, so anything absent here is
    a hard ingest failure rather than a slow first run.
    """

    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)
    report = ModelService.conversion_artifacts_report("Qwen/Qwen3-Embedding-0.6B")

    assert not report.ready
    assert report.missing
    assert "Qwen/Qwen3-Embedding-0.6B" in report.missing
    assert all(not artifact.present for artifact in report.artifacts)
    assert {artifact.repo for artifact in report.artifacts} == set(report.missing)


def test_conversion_artifacts_report_is_ready_once_every_repository_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)
    for repo, revision, _purpose in ModelService.required_conversion_artifacts(
        "Qwen/Qwen3-Embedding-0.6B"
    ):
        _cache_repo(tmp_path, repo, revision)

    report = ModelService.conversion_artifacts_report("Qwen/Qwen3-Embedding-0.6B")

    assert report.ready
    assert report.missing == []


def test_conversion_artifacts_report_rejects_an_interrupted_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)
    required = ModelService.required_conversion_artifacts("Qwen/Qwen3-Embedding-0.6B")
    for repo, revision, _purpose in required:
        _cache_repo(tmp_path, repo, revision, complete=repo != required[0][0])

    report = ModelService.conversion_artifacts_report("Qwen/Qwen3-Embedding-0.6B")

    assert not report.ready
    assert report.missing == [required[0][0]]


def test_required_conversion_artifacts_still_match_what_docling_resolves() -> None:
    """Drift guard against the pinned docling repositories.

    The list is data so readiness stays cheap, which means an upstream docling
    release could silently start resolving a different repository.  This test
    asks docling itself and fails when the two disagree.
    """

    pytest.importorskip("docling")
    import inspect

    from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions
    from docling.models.stages.table_structure.table_structure_model import (
        TableStructureModel,
    )

    # Only the default engine's repository is required: the per-engine
    # overrides (ONNX) are never fetched by the configured pipeline.
    spec = LayoutObjectDetectionOptions().model_spec
    expected = {(spec.repo_id, spec.revision)}

    pinned = {
        repo: revision
        for repo, revision, _purpose in ModelService.required_conversion_artifacts(
            "tokenizer/example"
        )
    }
    for repo, revision in expected:
        assert pinned.get(repo) == revision, f"docling now resolves {repo}@{revision}"

    # The table model hardcodes its repository inside a staticmethod body, so
    # the source is the only place to read it from.
    table_source = inspect.getsource(TableStructureModel.download_models)
    for repo, revision in pinned.items():
        if repo in table_source:
            assert revision in table_source
            break
    else:
        raise AssertionError("no pinned repository matches the docling table model")


def test_admission_probe_pdf_has_correct_cross_reference_offsets() -> None:
    """A malformed probe would fail before a single model is resolved.

    docling rejects the file during parsing, so the admission run would report
    failure while the cache is actually fine -- or worse, look like a missing
    model.  The offsets are the part that silently rots when the objects change.
    """

    data = ModelService._probe_pdf()
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")

    startxref = int(data.rsplit(b"startxref", 1)[1].split(b"%%EOF")[0].strip())
    assert data[startxref : startxref + 4] == b"xref"

    entries = data[startxref:].split(b"\n")[2:]
    offsets = [
        int(line.split()[0]) for line in entries if line.endswith(b"n ") or line.endswith(b"n")
    ]
    assert len(offsets) == 5
    for number, offset in enumerate(offsets, start=1):
        assert data[offset:].startswith(b"%d 0 obj" % number), f"object {number}"


def test_an_already_downloaded_pinned_artifact_gets_its_default_ref_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installations that predate the ref fix must not stay broken.

    The link is written after a download, so a cache filled by an earlier
    release still holds a snapshot reachable only by its commit.  Every offline
    worker -- import and query alike -- loads those models by name, so without
    a repair pass reranking keeps failing on exactly the machines that already
    paid for the download.
    """

    artifact = next(
        item
        for item in ModelService.curated_catalog().artifacts
        if item.provider.value == "hugging-face"
    )
    repo = tmp_path / ("models--" + artifact.model.replace("/", "--"))
    snapshot = repo / "snapshots" / artifact.revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)
    assert not (repo / "refs").exists()

    repaired = ModelService.repair_pinned_catalog_revisions()

    assert artifact.model in repaired
    assert (repo / "refs" / "main").read_text() == artifact.revision


def test_repair_skips_artifacts_that_were_never_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ModelService, "hugging_face_cache_root", lambda: tmp_path)

    assert ModelService.repair_pinned_catalog_revisions() == []
