from __future__ import annotations

from omarag_bridge.models.domain import (
    HardwareInfo,
    HardwareProfile,
    ModelCatalogEntry,
    ModelCategory,
    ModelFit,
    ModelSource,
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
    assert "BGE" in packages[2].synergy
