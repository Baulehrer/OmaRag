from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from omarag_bridge.adapters.base import (
    SearchManyItem,
    SearchManyRequest,
    SearchManyResult,
    SearchManyStats,
)
from omarag_bridge.adapters.isolated import (
    _QUERY_OPERATIONS,
    IsolatedHaikuAdapter,
    WorkerLimits,
    _ChildCallbacks,
    _ollama_targets,
    _unload_ollama_targets,
)
from omarag_bridge.services.resource_coordinator import MemorySnapshot, ResourceCoordinator


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


@pytest.mark.asyncio
async def test_query_residency_grows_with_reuse_and_drops_on_pressure(monkeypatch) -> None:
    coordinator = ResourceCoordinator()
    ready = MemorySnapshot(total=16_000, available=10_000, reserve=2_000)
    pressured = MemorySnapshot(total=16_000, available=3_000, reserve=2_000)
    monkeypatch.setattr(coordinator, "memory", lambda: ready)

    assert coordinator.residency_seconds() == 30.0
    expected = [60.0, 120.0, 240.0, 300.0, 300.0]
    for lifetime in expected:
        async with coordinator.chat():
            pass
        assert coordinator.residency_seconds() == lifetime

    monkeypatch.setattr(coordinator, "memory", lambda: pressured)
    assert coordinator.residency_seconds() == 0.0

    capped = ResourceCoordinator(max_residency_seconds=45.0)
    monkeypatch.setattr(capped, "memory", lambda: ready)
    async with capped.chat():
        pass
    assert capped.residency_seconds() == 45.0


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


@pytest.mark.asyncio
async def test_search_many_uses_one_worker_round_trip_and_reports_savings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    assert "search_many" in _QUERY_OPERATIONS
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def call(operation: str, *args: object, **kwargs: object) -> SearchManyResult:
        calls.append((operation, args, kwargs))
        return SearchManyResult(
            items=[SearchManyItem(key="F1", hits=[])],
            hydrated_chunks=[],
            stats=SearchManyStats(
                search_requests=2,
                successful_searches=2,
                backend_sessions=1,
                native_batch=True,
            ),
        )

    monkeypatch.setattr(adapter, "_call", call)
    requests = [
        SearchManyRequest(key="F1", query="first", limit=8, rerank=False),
        SearchManyRequest(key="F2", query="second", limit=8, rerank=False),
    ]

    result = await adapter.search_many(
        tmp_path / "knowledge.lancedb",
        requests,
        hydrate_chunk_ids=["route-1", "route-1"],
    )

    assert len(calls) == 1
    operation, args, kwargs = calls[0]
    assert operation == "search_many"
    assert args == (tmp_path / "knowledge.lancedb", requests)
    assert kwargs == {"hydrate_chunk_ids": ["route-1", "route-1"]}
    assert result.stats.ipc_round_trips == 1
    assert result.stats.ipc_round_trips_saved == 2


@pytest.mark.asyncio
async def test_search_many_contains_worker_failure_per_facet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = _adapter()

    async def call(*_args: object, **_kwargs: object) -> SearchManyResult:
        raise RuntimeError("worker unavailable")

    monkeypatch.setattr(adapter, "_call", call)
    result = await adapter.search_many(
        tmp_path / "knowledge.lancedb",
        [SearchManyRequest(key="F1", query="first", limit=8, rerank=False)],
        hydrate_chunk_ids=["route-1"],
    )

    assert result.items[0].failure is not None
    assert result.items[0].failure.code == "RuntimeError"
    assert result.hydration_failure is not None
    assert result.stats.ipc_round_trips == 1
    assert result.stats.fallback_reason == "worker_batch_failed"


def test_the_memory_watchdog_says_why_before_it_kills_the_worker(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """``os._exit`` skips buffers and handlers, so the reason has to be written
    first.  Without it the worker vanishes mid-request and the parent sees only
    a broken pipe -- which is how an intermittent retrieval failure stayed
    undiagnosed through an entire investigation."""

    from omarag_bridge.adapters import isolated

    monkeypatch.setattr(isolated, "_memory_usage", lambda: 4_000 * 1024**2)
    exits: list[int] = []
    monkeypatch.setattr(isolated.os, "_exit", lambda code: exits.append(code))

    stop = threading.Event()

    def release() -> None:
        # One pass is enough; the fake _exit does not stop the loop.
        stop.set()

    threading.Timer(0.6, release).start()
    isolated._memory_watchdog(3_584 * 1024**2, stop)

    assert exits and exits[0] == 137
    message = capfd.readouterr().err
    assert "memory" in message.casefold()
    assert "3584" in message and "4000" in message


def test_the_query_budget_grows_with_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """3584 MB was chosen for an 8 GB baseline and then applied everywhere.

    Exceeding it kills the worker outright, so on a larger machine the cap
    creates a failure mode out of memory that is sitting there unused.  The
    figure stays put on small machines: it is a floor, never a reduction.
    """

    from omarag_bridge import config

    monkeypatch.setattr(config, "_total_memory_mb", lambda: 8 * 1024)
    assert config.default_query_memory_max_mb() == 3584

    monkeypatch.setattr(config, "_total_memory_mb", lambda: 16 * 1024)
    assert config.default_query_memory_max_mb() > 3584

    monkeypatch.setattr(config, "_total_memory_mb", lambda: 4 * 1024)
    assert config.default_query_memory_max_mb() == 3584


def test_the_query_budget_never_crowds_out_the_answer_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama holds the chat model resident beside the worker; a budget that
    ignores it would trade a silent kill for a swapping machine."""

    from omarag_bridge import config

    monkeypatch.setattr(config, "_total_memory_mb", lambda: 64 * 1024)
    assert config.default_query_memory_max_mb() <= 64 * 1024 // 2
