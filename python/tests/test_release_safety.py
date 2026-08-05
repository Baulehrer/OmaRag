from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from omarag_bridge.app import create_app
from omarag_bridge.config import Settings
from omarag_bridge.main import parser, settings_from_args
from omarag_bridge.models.api import CreateWorkspaceRequest
from omarag_bridge.services.resource_coordinator import ResourceCoordinator
from omarag_bridge.services.workspace_service import WorkspaceService
from omarag_bridge.store import StateStore


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


async def test_speculative_warmup_never_waits_behind_foreground_work() -> None:
    resources = ResourceCoordinator()

    async with resources.indexing(), resources.warmup() as admission:
        assert admission == "skipped_busy"

    async with resources.warmup() as admission:
        assert admission == "ready"
