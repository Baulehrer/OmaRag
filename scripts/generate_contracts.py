"""Regenerate the checked-in API and JSON Schema contract snapshots."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from omarag_bridge.app import create_app
from omarag_bridge.config import Settings
from omarag_bridge.models.domain import WorkspaceManifest
from omarag_bridge.models.events import DomainEvent

ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="omarag-contract-") as temporary:
        app = create_app(Settings(data_dir=Path(temporary), auth_enabled=False))
        write_json(ROOT / "api" / "openapi.snapshot.json", app.openapi())
        app.state.services.store.close()
    write_json(
        ROOT / "api" / "events.schema.json",
        DomainEvent.model_json_schema(mode="serialization"),
    )
    write_json(
        ROOT / "api" / "workspace.schema.json",
        WorkspaceManifest.model_json_schema(mode="serialization"),
    )


if __name__ == "__main__":
    main()
