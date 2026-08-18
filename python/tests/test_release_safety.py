from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from omarag_bridge.app import create_app
from omarag_bridge.config import Settings
from omarag_bridge.main import parser, settings_from_args
from omarag_bridge.models.api import CreateWorkspaceRequest
from omarag_bridge.models.domain import JobStatus
from omarag_bridge.models.errors import ConflictError
from omarag_bridge.runtime import configure_process_environment
from omarag_bridge.services.job_service import JobService
from omarag_bridge.services.resource_coordinator import ResourceCoordinator
from omarag_bridge.services.workspace_service import WorkspaceService
from omarag_bridge.store import StateStore


def test_retired_branding_is_absent() -> None:
    root = Path(__file__).resolve().parents[2]
    retired_name = "dae" + "dalus"
    retired_ligature = "dæ" + "dalus"
    blocked = (
        f"oracle of {retired_name}",
        f"oracle of {retired_ligature}",
        f"oracle-of-{retired_name}",
        f"oracle-{retired_name}",
    )
    text_suffixes = {
        ".desktop",
        ".in",
        ".json",
        ".md",
        ".py",
        ".rs",
        ".sh",
        ".toml",
        ".yaml",
        ".yml",
    }
    violations: list[str] = []
    for base in (".github", "deploy", "docs", "python", "rust", "scripts"):
        for path in (root / base).rglob("*"):
            if not path.is_file() or path.name == "rerank_proxy.py":
                continue
            relative = path.relative_to(root)
            if any(part in {".venv", "__pycache__"} for part in relative.parts):
                continue
            lowered_path = str(relative).casefold()
            if any(value in lowered_path for value in blocked):
                violations.append(lowered_path)
                continue
            if path.suffix.casefold() not in text_suffixes:
                continue
            content = path.read_text(encoding="utf-8").casefold()
            if any(value in content for value in blocked):
                violations.append(lowered_path)
    readme = (root / "README.md").read_text(encoding="utf-8").casefold()
    if any(value in readme for value in blocked):
        violations.append("readme.md")
    assert violations == []


def test_offline_worker_environment_removes_proxy_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("all_proxy", "socks5://proxy.invalid:1080")

    configure_process_environment(offline_models=True)

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["DO_NOT_TRACK"] == "1"
    assert os.environ["OLLAMA_NO_CLOUD"] == "1"
    assert "HTTPS_PROXY" not in os.environ
    assert "all_proxy" not in os.environ


def test_generated_token_is_persistent_and_private(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", auth_enabled=True)
    first = create_app(settings)
    token = first.state.services.token
    token_path = first.state.services.token_path
    first.state.services.store.close()

    assert token
    assert token_path == settings.data_dir / "auth-token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600

    second = create_app(settings)
    try:
        assert second.state.services.token == token
    finally:
        second.state.services.store.close()


def test_cli_preserves_environment_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMARAG_BEARER_TOKEN", "from-environment")
    settings = settings_from_args(parser().parse_args([]))

    assert settings.bearer_token == "from-environment"


def test_workspace_uses_configured_ollama_endpoint(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    service = WorkspaceService(tmp_path / "workspaces", store, "http://host.docker.internal:11434")
    workspace = service.create(CreateWorkspaceRequest(name="Container"))
    config = (Path(workspace.path) / "haiku.rag.yaml").read_text(encoding="utf-8")
    store.close()

    assert "base_url: http://host.docker.internal:11434" in config
    assert "max_tokens:" in config
    assert "reasoning_effort: none" in config


def test_physical_delete_restores_directory_when_store_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    service = WorkspaceService(tmp_path / "workspaces", store)
    workspace = service.create(CreateWorkspaceRequest(name="Keep me"))
    workspace_path = Path(workspace.path)

    def fail(_: str) -> None:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(store, "remove_workspace", fail)
    with pytest.raises(RuntimeError, match="database failure"):
        service.delete(workspace.id, physical=True)

    assert workspace_path.is_dir()
    store.close()


async def test_chat_gets_next_heavy_resource_slot() -> None:
    resources = ResourceCoordinator()
    order: list[str] = []
    release = asyncio.Event()

    async def active_index() -> None:
        async with resources.indexing():
            order.append("index-1")
            await release.wait()

    async def waiting_index() -> None:
        async with resources.indexing():
            order.append("index-2")

    async def chat() -> None:
        async with resources.chat():
            order.append("chat")

    first = asyncio.create_task(active_index())
    await asyncio.sleep(0)
    second = asyncio.create_task(waiting_index())
    question = asyncio.create_task(chat())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second, question)

    assert order == ["index-1", "chat", "index-2"]


async def test_a_second_conversion_only_starts_when_memory_clearly_allows_it() -> None:
    """Parallel conversion is a memory bet; it must be refused unless it is safe.

    Two conversions roughly double the peak, so a second slot requires free
    memory covering that peak twice over on top of the reserve.
    """
    from omarag_bridge.services import resource_coordinator as coordinator_module
    from omarag_bridge.services.resource_coordinator import MemorySnapshot

    resources = ResourceCoordinator()
    peak = coordinator_module._CONVERSION_PEAK_BYTES

    # Plenty of room: a second conversion is allowed.
    resources.memory = lambda: MemorySnapshot(  # type: ignore[method-assign]
        total=64 * 1024**3, available=peak * 2 + 8 * 1024**3, reserve=1024**3
    )
    assert resources.conversion_slots() == 2

    # Tight: fall back to one at a time.
    resources.memory = lambda: MemorySnapshot(  # type: ignore[method-assign]
        total=8 * 1024**3, available=peak, reserve=1024**3
    )
    assert resources.conversion_slots() == 1

    # Under pressure the answer is one, whatever the arithmetic says.
    resources.memory = lambda: MemorySnapshot(  # type: ignore[method-assign]
        total=8 * 1024**3, available=1024**3, reserve=1024**3
    )
    assert resources.conversion_slots() == 1


async def test_conversions_overlap_only_up_to_the_permitted_slots() -> None:
    from omarag_bridge.services.resource_coordinator import MemorySnapshot

    resources = ResourceCoordinator()
    resources.memory = lambda: MemorySnapshot(  # type: ignore[method-assign]
        total=64 * 1024**3, available=60 * 1024**3, reserve=1024**3
    )
    peak_seen = 0
    release = asyncio.Event()

    async def convert() -> None:
        nonlocal peak_seen
        async with resources.indexing():
            peak_seen = max(peak_seen, resources.active_indexers)
            await release.wait()

    tasks = [asyncio.create_task(convert()) for _ in range(3)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*tasks)

    assert peak_seen == 2, "two slots were granted, so two must overlap — and no more"


async def test_a_question_still_excludes_indexing_entirely() -> None:
    from omarag_bridge.services.resource_coordinator import MemorySnapshot

    resources = ResourceCoordinator()
    resources.memory = lambda: MemorySnapshot(  # type: ignore[method-assign]
        total=64 * 1024**3, available=60 * 1024**3, reserve=1024**3
    )
    overlapped = False
    release = asyncio.Event()

    async def question() -> None:
        async with resources.chat():
            await release.wait()

    async def convert() -> None:
        nonlocal overlapped
        async with resources.indexing():
            overlapped = True

    asking = asyncio.create_task(question())
    await asyncio.sleep(0)
    converting = asyncio.create_task(convert())
    await asyncio.sleep(0)
    assert not overlapped, "indexing must never run beside an active question"
    release.set()
    await asyncio.gather(asking, converting)
    assert overlapped


async def test_speculative_warmup_never_waits_behind_foreground_work() -> None:
    resources = ResourceCoordinator()

    async with resources.indexing(), resources.warmup() as admission:
        assert admission == "skipped_busy"

    async with resources.warmup() as admission:
        assert admission == "ready"


async def test_config_writer_fails_before_waiting_on_a_paused_job() -> None:
    service = JobService.__new__(JobService)
    service._admission_lock = asyncio.Lock()
    service._writer_lock = asyncio.Lock()
    current = asyncio.current_task()
    assert current is not None
    service._tasks = {"paused-job": current}
    await service._writer_lock.acquire()

    try:
        with pytest.raises(ConflictError, match="queued, running, or paused"):
            async with asyncio.timeout(0.1):
                async with service.writer(fail_if_active=True):
                    pytest.fail("active job must reject configuration admission")
    finally:
        service._writer_lock.release()


async def test_pause_checkpoint_does_not_wait_while_holding_writer_lock() -> None:
    class Store:
        def __init__(self) -> None:
            self.job = type(
                "Job",
                (),
                {
                    "id": "job-1",
                    "workspace_id": "workspace-1",
                    "status": JobStatus.PAUSE_REQUESTED,
                },
            )()

        def get_job(self, _job_id: str):
            return self.job

        def update_job(self, _job_id: str, **updates):
            for key, value in updates.items():
                setattr(self.job, key, value)
            return self.job

    class Events:
        async def emit(self, *_args, **_kwargs) -> None:
            return None

    service = JobService.__new__(JobService)
    service.store = Store()
    service.events = Events()

    async with asyncio.timeout(0.1):
        assert await service._continue("job-1") is False
    assert service.store.job.status.value == "paused"
