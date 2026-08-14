from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omarag_bridge.adapters.isolated import (
    IsolatedHaikuAdapter,
    WorkerLimits,
    _ChildCallbacks,
    _ollama_targets,
    _unload_ollama_targets,
)


def _adapter(*, idle: float = 0.1) -> IsolatedHaikuAdapter:
    mib = 1024**2
    return IsolatedHaikuAdapter(
        api_limits=WorkerLimits(256 * mib, 512 * mib, 0),
        import_limits=WorkerLimits(1024 * mib, 2048 * mib, 256 * mib),
        query_limits=WorkerLimits(1024 * mib, 2048 * mib, 256 * mib),
        utility_limits=WorkerLimits(512 * mib, 1536 * mib, 128 * mib),
        query_idle_seconds=idle,
        unload_ollama_models=False,
    )


def test_adapter_metadata_does_not_import_haiku_client() -> None:
    before = {name for name in sys.modules if name.startswith("haiku.")}

    adapter = _adapter()

    assert adapter.version
    assert adapter.available
    assert {name for name in sys.modules if name.startswith("haiku.")} == before


def test_isolated_adapter_exposes_verified_v2_capabilities() -> None:
    adapter = _adapter()
    if adapter.version != "0.74.0":
        pytest.skip("Book-v2 capability contract is pinned to Haiku 0.74.0")
    assert adapter.capabilities.book_index_v2 is True
    assert adapter.capabilities.adaptive_retrieval is True
    assert adapter.capabilities.claim_streaming is True
    assert adapter.capabilities.knowledge_snapshots is True
    assert adapter.capabilities.streaming_chat is True


def test_config_validation_runs_outside_parent_process() -> None:
    adapter = _adapter()
    if not adapter.available:
        pytest.skip("Haiku optional dependency is not installed")
    before = {name for name in sys.modules if name.startswith("haiku.")}

    adapter.validate_config("environment: production\n")

    assert {name for name in sys.modules if name.startswith("haiku.")} == before


@pytest.mark.asyncio
async def test_utility_worker_exits_after_database_creation(tmp_path: Path) -> None:
    adapter = _adapter()
    if not adapter.available:
        pytest.skip("Haiku optional dependency is not installed")

    await adapter.ensure_database(tmp_path / "database")

    assert adapter._query_worker is None
    await adapter.shutdown()


def test_query_cleanup_discovers_only_used_ollama_models(tmp_path: Path) -> None:
    workspace = tmp_path / "library.omarag"
    database = workspace / "database" / "knowledge.lancedb"
    database.parent.mkdir(parents=True)
    (workspace / "haiku.rag.yaml").write_text(
        """embeddings:
  model: {provider: ollama, name: embed-local}
reranking:
  model: {provider: cross-encoder, name: local-reranker}
qa:
  model: {provider: ollama, name: answer-local}
providers:
  ollama: {base_url: http://localhost:11434}
""",
        encoding="utf-8",
    )

    assert _ollama_targets(database, "search", "http://fallback") == {
        ("http://localhost:11434", "embed-local")
    }
    assert _ollama_targets(database, "ask", "http://fallback") == {
        ("http://localhost:11434", "embed-local"),
        ("http://localhost:11434", "answer-local"),
    }


def test_query_cleanup_uses_official_keep_alive_zero_request(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            pass

        @staticmethod
        def read() -> bytes:
            return b"{}"

    def urlopen(request, timeout: int):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    _unload_ollama_targets({("http://localhost:11434", "embed-local")})

    assert captured == {
        "url": "http://localhost:11434/api/generate",
        "body": b'{"model": "embed-local", "keep_alive": 0}',
        "timeout": 5,
    }


async def test_worker_forwards_index_phase_callbacks() -> None:
    class Connection:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send(self, message: dict[str, object]) -> None:
            self.sent.append(message)

        def recv(self) -> dict[str, object]:
            return {
                "type": "callback_result",
                "id": self.sent[-1]["id"],
                "result": None,
            }

    connection = Connection()
    callbacks = _ChildCallbacks(connection, {"on_phase"})

    await callbacks.options()["on_phase"]("embedding", 26, 50, 300)

    assert connection.sent[-1]["name"] == "on_phase"
    assert connection.sent[-1]["args"] == ("embedding", 26, 50, 300)
