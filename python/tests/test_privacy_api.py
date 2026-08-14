from __future__ import annotations

from pathlib import Path

import httpx2
from fastapi import FastAPI


async def test_workspace_privacy_routes_use_etags_and_persist_safely(
    client: httpx2.AsyncClient,
    app: FastAPI,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    current = await client.get(f"/v1/workspaces/{workspace_id}/privacy")
    assert current.status_code == 200
    assert current.json()["mode"] == "device-only"
    assert current.headers["cache-control"] == "no-store"

    policy = {
        "mode": "trusted-endpoint",
        "trusted_endpoints": ["https://rag.example.test"],
        "cloud_acknowledged": False,
    }
    stale = await client.put(
        f"/v1/workspaces/{workspace_id}/privacy",
        headers={"If-Match": '"stale"'},
        json={"policy": policy},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "ETAG_CONFLICT"

    updated = await client.put(
        f"/v1/workspaces/{workspace_id}/privacy",
        headers={"If-Match": current.headers["etag"]},
        json={"policy": policy},
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] != current.headers["etag"]
    assert updated.json()["trusted_endpoints"] == ["https://rag.example.test"]

    persisted = await client.get(f"/v1/workspaces/{workspace_id}/privacy")
    assert persisted.json() == updated.json()
    root = await client.get(f"/v1/workspaces/{workspace_id}")
    assert root.json()["privacy_mode"] == "trusted-endpoint"
    policy_file = Path(str(workspace["path"])) / ".omarag" / "privacy-policy.json"
    workspace_path = Path(str(workspace["path"]))
    assert workspace_path.stat().st_mode & 0o777 == 0o700
    assert (workspace_path / "haiku.rag.yaml").stat().st_mode & 0o777 == 0o600
    assert app.state.services.store.path.stat().st_mode & 0o777 == 0o600
    assert policy_file.parent.stat().st_mode & 0o777 == 0o700
    assert policy_file.stat().st_mode & 0o777 == 0o600

    event = next(
        item
        for item in app.state.services.store.events_after(0, workspace_id=workspace_id)
        if item.type == "workspace.privacy.changed"
    )
    assert event.payload["trusted_endpoint_count"] == 1
    assert "rag.example.test" not in event.model_dump_json()


async def test_retention_routes_plan_and_execute_only_confirmed_empty_cleanup(
    client: httpx2.AsyncClient,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    current = await client.get(f"/v1/workspaces/{workspace_id}/retention")
    assert current.status_code == 200
    assert current.json()["profile"] == "minimal"
    assert current.json()["event_hours"] == 24

    invalid_legacy = await client.put(
        f"/v1/workspaces/{workspace_id}/retention",
        headers={"If-Match": current.headers["etag"]},
        json={"policy": {"profile": "legacy"}},
    )
    assert invalid_legacy.status_code == 422

    updated = await client.put(
        f"/v1/workspaces/{workspace_id}/retention",
        headers={"If-Match": current.headers["etag"]},
        json={"policy": {"answer_cache_days": 5}},
    )
    assert updated.status_code == 200
    assert updated.json()["answer_cache_days"] == 5
    assert updated.headers["etag"] != current.headers["etag"]

    preflight = await client.post(f"/v1/workspaces/{workspace_id}/retention/cleanup/preflight")
    assert preflight.status_code == 200
    assert preflight.json()["dry_run"] is True
    assert preflight.json()["eligible_records"] == 0
    assert preflight.headers["cache-control"] == "no-store"

    unconfirmed = await client.post(
        f"/v1/workspaces/{workspace_id}/retention/cleanup",
        headers={"If-Match": preflight.headers["etag"]},
        json={"plan_id": preflight.json()["plan_id"], "confirm": "NO"},
    )
    assert unconfirmed.status_code == 422

    cleaned = await client.post(
        f"/v1/workspaces/{workspace_id}/retention/cleanup",
        headers={"If-Match": preflight.headers["etag"]},
        json={
            "plan_id": preflight.json()["plan_id"],
            "confirm": "PURGE_EXPIRED",
        },
    )
    assert cleaned.status_code == 200
    assert sum(cleaned.json()["purged_records"].values()) == 0
    assert cleaned.headers["etag"] != preflight.headers["etag"]

    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/retention/cleanup",
        headers={"If-Match": cleaned.headers["etag"]},
        json={
            "plan_id": preflight.json()["plan_id"],
            "confirm": "PURGE_EXPIRED",
        },
    )
    assert replay.status_code == 404

    locked = await client.patch(
        f"/v1/workspaces/{workspace_id}",
        headers={"If-Match": cleaned.headers["etag"]},
        json={"read_only": True},
    )
    assert locked.status_code == 200
    blocked = await client.put(
        f"/v1/workspaces/{workspace_id}/retention",
        headers={"If-Match": locked.headers["etag"]},
        json={"policy": {"answer_cache_days": 4}},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "WORKSPACE_READ_ONLY"


async def test_device_only_url_import_is_denied_before_job_creation(
    client: httpx2.AsyncClient,
    app: FastAPI,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    private_url = "https://books.example.test/private.pdf?token=secret"
    denied = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        headers={"Idempotency-Key": "remote-denied"},
        json={"sources": [{"type": "url", "path": private_url}]},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "EGRESS_DENIED"
    assert "books.example.test" not in denied.text
    assert "secret" not in denied.text
    assert app.state.services.store.list_jobs(workspace_id) == []

    preflight = await client.post(
        f"/v1/workspaces/{workspace_id}/imports/preflight",
        json={"sources": [{"type": "url", "path": private_url}]},
    )
    assert preflight.status_code == 403

    privacy = await client.get(f"/v1/workspaces/{workspace_id}/privacy")
    allowed = await client.put(
        f"/v1/workspaces/{workspace_id}/privacy",
        headers={"If-Match": privacy.headers["etag"]},
        json={
            "policy": {
                "mode": "cloud-allowed",
                "cloud_acknowledged": True,
            }
        },
    )
    assert allowed.status_code == 200
    started = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        headers={"Idempotency-Key": "remote-allowed"},
        json={"sources": [{"type": "url", "path": private_url}]},
    )
    assert started.status_code == 202


async def test_device_only_rejects_remote_model_runtime_before_job_creation(
    client: httpx2.AsyncClient,
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
) -> None:
    workspace_id = str(workspace["id"])
    source = tmp_path / "private-book.txt"
    source.write_text("private textbook content", encoding="utf-8")
    app.state.services.workspaces.ollama_url = "https://remote-model.example.test"

    denied = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        headers={"Idempotency-Key": "remote-model-denied"},
        json={"sources": [{"type": "file", "path": str(source)}]},
    )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "EGRESS_DENIED"
    assert "remote-model.example.test" not in denied.text
    assert app.state.services.store.list_jobs(workspace_id) == []
