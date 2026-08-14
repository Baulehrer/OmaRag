from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omarag_bridge.adapters.haiku_v070 import VanillaHaikuAdapter
from omarag_bridge.models.domain import SearchHit
from omarag_bridge.services import reranker_service
from omarag_bridge.services.query_v2 import FusedCandidate, RetrievalCandidate
from omarag_bridge.services.reranker_service import PersistentCrossEncoder


async def test_persistent_reranker_loads_once_and_keeps_breadcrumbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[list[str]]] = []

    class FakeModel:
        def predict(self, pairs, **_options):
            calls.append(pairs)
            return [2.0 for _ in pairs]

    reranker = PersistentCrossEncoder(model_name="test/model")
    monkeypatch.setattr(reranker, "_load", lambda: FakeModel())
    candidate = RetrievalCandidate(
        chunk_id="chunk-1",
        content="Der Wasserzementwert ist maßgebend.",
        headings=("Beton", "Zusammensetzung"),
    )
    fused = FusedCandidate(candidate, 0.1, (("hybrid", 1),), ("hybrid",))

    first = await reranker.score("Wasserzementwert", [fused])
    second = await reranker.score("Wasserzementwert", [fused])

    assert first[0][1] == 2.0
    assert second[0][1] == 2.0
    assert reranker.loaded is True
    assert calls[0][0][1].startswith("Beton › Zusammensetzung\n")


async def test_adapter_forwards_configured_immutable_reranker_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, str | None]] = []

    class FakeCrossEncoder:
        def __init__(self, model_name: str, revision: str | None) -> None:
            created.append((model_name, revision))

        async def score(self, _question: str, candidates):
            return [(item.candidate, 0.75) for item in candidates]

    adapter = VanillaHaikuAdapter()
    monkeypatch.setattr(
        adapter,
        "_config",
        lambda _database: SimpleNamespace(
            reranking=SimpleNamespace(
                model=SimpleNamespace(
                    provider="cross-encoder",
                    name="example/expert-reranker",
                    revision="0123456789abcdef",
                )
            )
        ),
    )
    monkeypatch.setattr(reranker_service, "PersistentCrossEncoder", FakeCrossEncoder)

    scores = await adapter.rerank(
        Path("unused.lancedb"),
        "Frage",
        [SearchHit(chunk_id="chunk-1", content="Beleg")],
    )

    assert scores == [0.75]
    assert created == [("example/expert-reranker", "0123456789abcdef")]


@pytest.mark.parametrize(
    ("explicit_revision", "expected_revision"),
    [("explicit-commit", "explicit-commit"), (None, "profile-commit")],
)
async def test_adapter_recovers_reranker_pin_from_raw_haiku_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_revision: str | None,
    expected_revision: str,
) -> None:
    workspace = tmp_path / "raw-pin.omarag"
    database = workspace / "database" / "knowledge.lancedb"
    database.parent.mkdir(parents=True)
    revision_line = f"    revision: {explicit_revision}\n" if explicit_revision is not None else ""
    (workspace / "haiku.rag.yaml").write_text(
        "reranking:\n"
        "  model:\n"
        "    provider: cross-encoder\n"
        "    name: example/pinned-reranker\n"
        f"{revision_line}"
        "oracle:\n"
        "  model_profile:\n"
        "    expert_mode: false\n"
        "    artifacts:\n"
        "      rerank:\n"
        "        provider: hugging-face\n"
        "        model: example/pinned-reranker\n"
        "        revision: profile-commit\n",
        encoding="utf-8",
    )
    created: list[tuple[str, str | None]] = []

    class FakeCrossEncoder:
        def __init__(self, model_name: str, revision: str | None) -> None:
            created.append((model_name, revision))

        async def score(self, _question: str, candidates):
            return [(item.candidate, 0.5) for item in candidates]

    adapter = VanillaHaikuAdapter()
    # Mirrors Haiku 0.74: the validated model object has dropped the YAML-only
    # revision extra, so the adapter must recover it from the raw config.
    monkeypatch.setattr(
        adapter,
        "_config",
        lambda _database: SimpleNamespace(
            reranking=SimpleNamespace(
                model=SimpleNamespace(
                    provider="cross-encoder",
                    name="example/pinned-reranker",
                )
            )
        ),
    )
    monkeypatch.setattr(reranker_service, "PersistentCrossEncoder", FakeCrossEncoder)

    await adapter.rerank(
        database,
        "Frage",
        [SearchHit(chunk_id="chunk-raw", content="Beleg")],
    )

    assert created == [("example/pinned-reranker", expected_revision)]


async def test_adapter_rejects_uncalibrated_custom_reranker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = VanillaHaikuAdapter()
    monkeypatch.setattr(
        adapter,
        "_config",
        lambda _database: SimpleNamespace(
            reranking=SimpleNamespace(
                model=SimpleNamespace(
                    provider="cross-encoder",
                    name="example/unpinned-reranker",
                )
            )
        ),
    )

    with pytest.raises(RuntimeError, match="immutable local revision pin"):
        await adapter.rerank(
            tmp_path / "database" / "knowledge.lancedb",
            "Frage",
            [SearchHit(chunk_id="chunk-unpinned", content="Beleg")],
        )
