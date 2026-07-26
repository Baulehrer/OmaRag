from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI

from omarag_bridge.models.domain import WorkspaceManifest
from omarag_bridge.models.events import DomainEvent

ROOT = Path(__file__).resolve().parents[2]


def load(name: str) -> object:
    return json.loads((ROOT / "api" / name).read_text(encoding="utf-8"))


def test_openapi_snapshot_is_current(app: FastAPI) -> None:
    assert app.openapi() == load("openapi.snapshot.json")


def test_event_schema_snapshot_is_current() -> None:
    assert DomainEvent.model_json_schema(mode="serialization") == load("events.schema.json")


def test_workspace_schema_snapshot_is_current() -> None:
    assert WorkspaceManifest.model_json_schema(mode="serialization") == load(
        "workspace.schema.json"
    )
