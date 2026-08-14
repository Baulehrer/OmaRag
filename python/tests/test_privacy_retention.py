from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omarag_bridge.models.api import CreateWorkspaceRequest, SourceInput
from omarag_bridge.models.domain import (
    EgressPayloadClass,
    EgressReasonCode,
    JobStatus,
    PrivacyMode,
    PrivacyPolicy,
    RetentionCategory,
    RetentionPolicy,
    RetentionProfile,
    WorkspaceManifest,
)
from omarag_bridge.services.egress_policy import EgressPolicy, EgressPolicyError
from omarag_bridge.services.workspace_service import WorkspaceService
from omarag_bridge.store import StateStore


def test_legacy_local_alias_is_read_but_serialized_canonically() -> None:
    assert PrivacyMode("local") is PrivacyMode.DEVICE_ONLY
    assert PrivacyMode.LOCAL is PrivacyMode.DEVICE_ONLY
    assert PrivacyMode("trusted-endpoints") is PrivacyMode.TRUSTED_ENDPOINT
    assert PrivacyPolicy(mode="local").model_dump(mode="json")["mode"] == "device-only"


def test_egress_policy_fails_closed_without_leaking_url_or_content() -> None:
    policy = EgressPolicy()
    assert policy.authorize_http("http://127.0.0.1:11434/api/embed").allowed is True
    local_url_source = policy.evaluate_http(
        "http://127.0.0.1:8000/book.pdf", EgressPayloadClass.URL_SOURCE
    )
    assert local_url_source.allowed is False
    assert local_url_source.reason_code is EgressReasonCode.DENY_DEVICE_ONLY

    private_url = "https://user:password@example.invalid/api?q=/private/book.pdf"
    decision = policy.evaluate_http(private_url)
    assert decision.allowed is False
    assert decision.reason_code is EgressReasonCode.DENY_INVALID_ENDPOINT
    serialized = decision.model_dump_json()
    assert "example.invalid" not in serialized
    assert "password" not in serialized
    assert "/private/book.pdf" not in serialized

    with pytest.raises(EgressPolicyError) as captured:
        policy.authorize_http(private_url)
    public_error = json.dumps(captured.value.details)
    assert private_url not in str(captured.value)
    assert "example.invalid" not in public_error

    with pytest.raises(ValidationError, match="local path"):
        SourceInput(type="file", path="ftp://example.invalid/private.pdf")


def test_trusted_and_cloud_egress_require_explicit_authority() -> None:
    trusted = EgressPolicy(
        PrivacyPolicy(
            mode=PrivacyMode.TRUSTED_ENDPOINT,
            trusted_endpoints=["https://rag.example.test"],
        )
    )
    assert trusted.authorize_http("https://rag.example.test/v1/rerank").allowed is True
    lookalike = trusted.evaluate_http("https://rag.example.test.attacker.invalid/v1/rerank")
    assert lookalike.allowed is False
    assert lookalike.reason_code is EgressReasonCode.DENY_UNTRUSTED

    with pytest.raises(ValueError, match="requires HTTPS"):
        EgressPolicy(
            PrivacyPolicy(
                mode=PrivacyMode.TRUSTED_ENDPOINT,
                trusted_endpoints=["http://192.0.2.10:11434"],
            )
        )

    pending_cloud = EgressPolicy(PrivacyPolicy(mode=PrivacyMode.CLOUD_ALLOWED))
    assert (
        pending_cloud.evaluate_http("https://api.example.test/v1/chat").reason_code
        is EgressReasonCode.DENY_CLOUD_ACK_REQUIRED
    )
    assert pending_cloud.authorize_http(
        "https://api.example.test/v1/models", EgressPayloadClass.CONTROL_PLANE
    ).allowed
    acknowledged = EgressPolicy(
        PrivacyPolicy(mode=PrivacyMode.CLOUD_ALLOWED, cloud_acknowledged=True)
    )
    assert acknowledged.authorize_http("https://api.example.test/v1/chat").allowed


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/book.pdf",
        "https://2130706433/book.pdf",
        "https://127.1/book.pdf",
        "https://10.23.4.5/book.pdf",
        "https://169.254.169.254/latest/meta-data/",
        "https://192.0.2.8/book.pdf",
        "https://[::1]/book.pdf",
        "https://[fd00::10]/book.pdf",
        "https://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_url_sources_require_an_exact_allowlisted_origin_for_non_public_targets(
    url: str,
) -> None:
    cloud = EgressPolicy(PrivacyPolicy(mode=PrivacyMode.CLOUD_ALLOWED, cloud_acknowledged=True))

    decision = cloud.evaluate_http(url, EgressPayloadClass.URL_SOURCE)

    assert decision.allowed is False
    assert decision.reason_code is EgressReasonCode.DENY_UNTRUSTED


def test_exact_allowlisted_origin_can_import_a_non_public_url_source() -> None:
    cloud = EgressPolicy(
        PrivacyPolicy(
            mode=PrivacyMode.CLOUD_ALLOWED,
            cloud_acknowledged=True,
            trusted_endpoints=["https://10.23.4.5:8443"],
        )
    )

    assert cloud.authorize_http(
        "https://10.23.4.5:8443/books/manual.pdf", EgressPayloadClass.URL_SOURCE
    ).allowed
    assert not cloud.evaluate_http(
        "https://10.23.4.5/books/manual.pdf", EgressPayloadClass.URL_SOURCE
    ).allowed
    assert not cloud.evaluate_http(
        "https://10.23.4.6:8443/books/manual.pdf", EgressPayloadClass.URL_SOURCE
    ).allowed
    assert cloud.authorize_http(
        "https://8.8.8.8/books/manual.pdf", EgressPayloadClass.URL_SOURCE
    ).allowed


def test_retention_legacy_mode_requires_deliberate_opt_in() -> None:
    policy = RetentionPolicy()
    assert policy.profile is RetentionProfile.MINIMAL
    assert policy.event_hours == 24
    assert policy.terminal_run_days == 30
    assert policy.evaluation_limit == 10

    with pytest.raises(ValidationError, match="explicit opt-in"):
        RetentionPolicy(profile=RetentionProfile.LEGACY)
    legacy = RetentionPolicy(profile=RetentionProfile.LEGACY, legacy_opt_in=True)
    assert legacy.answer_cache_days is None
    assert legacy.event_hours is None
    assert legacy.evaluation_limit is None


def test_existing_workspace_is_migrated_to_legacy_retention(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    manifest = WorkspaceManifest(
        id="legacy-workspace",
        name="Legacy",
        path=str(tmp_path / "legacy.omarag"),
        etag="legacy-etag",
    )
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE workspaces (
               id TEXT PRIMARY KEY,
               manifest_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?)",
        (
            manifest.id,
            manifest.model_dump_json(),
            manifest.created_at.isoformat(),
            manifest.updated_at.isoformat(),
        ),
    )
    connection.commit()
    connection.close()

    store = StateStore(database)
    migrated = store.get_retention_policy(manifest.id)
    assert migrated.profile is RetentionProfile.LEGACY
    assert migrated.legacy_opt_in is True
    assert store.plan_retention_cleanup(manifest.id).eligible_records == 0
    store.close()


def test_cleanup_plan_is_private_and_rejection_never_deletes(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Retention")
    )
    assert store.get_retention_policy(workspace.id).profile is RetentionProfile.MINIMAL

    reference = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    old = (reference - timedelta(days=60)).isoformat()
    store.cache_answer(
        cache_key="private-cache-key",
        workspace_id=workspace.id,
        index_fingerprint="index",
        config_fingerprint="config",
        request={"question": "RAW PRIVATE QUESTION", "path": "/private/book.pdf"},
        answer="RAW PRIVATE ANSWER",
        citations=[],
        max_entries=64,
    )
    completed_run = store.create_run(
        "run-completed",
        workspace.id,
        {
            "session_id": "session-completed",
            "question": "RAW PRIVATE QUESTION",
            "evidence_mode": "strict",
        },
    )
    store.update_run(completed_run.id, status=JobStatus.COMPLETED)
    store.create_run(
        "run-active",
        workspace.id,
        {
            "session_id": "session-active",
            "question": "RAW ACTIVE QUESTION",
            "evidence_mode": "strict",
        },
    )
    completed_job, _ = store.create_job_idempotent(
        job_id="job-completed",
        workspace_id=workspace.id,
        kind="ingest",
        payload={"path": "/private/book.pdf"},
        idempotency_key="completed-key",
    )
    store.update_job(completed_job.id, status=JobStatus.COMPLETED)
    active_job, _ = store.create_job_idempotent(
        job_id="job-active",
        workspace_id=workspace.id,
        kind="ingest",
        payload={"path": "/private/active.pdf"},
        idempotency_key="active-key",
    )
    store.append_event(
        event_type="private.event",
        correlation_id="correlation",
        payload={"text": "RAW EVENT CONTENT"},
        workspace_id=workspace.id,
        run_id=completed_run.id,
    )
    store.save_import_preflight("preflight-old", workspace.id, {"path": "/private/book.pdf"})
    for index in range(12):
        store.save_evaluation(
            f"evaluation-{index:02d}", workspace.id, {"question": "RAW EVALUATION"}
        )

    with store._lock:
        store._db.execute(
            "UPDATE answer_cache SET last_used_at = ? WHERE workspace_id = ?",
            (old, workspace.id),
        )
        store._db.execute(
            "UPDATE runs SET updated_at = ? WHERE workspace_id = ?",
            (old, workspace.id),
        )
        store._db.execute(
            "UPDATE jobs SET updated_at = ? WHERE workspace_id = ?",
            (old, workspace.id),
        )
        store._db.execute(
            """UPDATE idempotency_keys SET created_at = ?
               WHERE result_id IN (?, ?)""",
            (old, completed_job.id, active_job.id),
        )
        store._db.execute(
            "UPDATE events SET timestamp = ? WHERE workspace_id = ?",
            (old, workspace.id),
        )
        store._db.execute(
            "UPDATE import_preflights SET created_at = ? WHERE workspace_id = ?",
            (old, workspace.id),
        )

    plan = store.plan_retention_cleanup(workspace.id, now=reference)
    actions = {action.category: action for action in plan.actions}
    assert actions[RetentionCategory.ANSWER_CACHE].eligible_records == 1
    assert actions[RetentionCategory.EVENTS].eligible_records == 1
    assert actions[RetentionCategory.RUNS].eligible_records == 1
    assert actions[RetentionCategory.RUNS].protected_records == 1
    assert actions[RetentionCategory.JOBS].eligible_records == 1
    assert actions[RetentionCategory.JOBS].protected_records == 1
    assert actions[RetentionCategory.IDEMPOTENCY_KEYS].eligible_records == 1
    assert actions[RetentionCategory.IDEMPOTENCY_KEYS].protected_records == 1
    assert actions[RetentionCategory.EVALUATIONS].eligible_records == 2
    assert actions[RetentionCategory.IMPORT_PREFLIGHTS].eligible_records == 1

    telemetry = plan.model_dump_json()
    assert "RAW PRIVATE" not in telemetry
    assert "/private/" not in telemetry
    with pytest.raises(ValueError, match="PURGE_EXPIRED"):
        store.purge_retention_cleanup(plan, confirmation="NO", now=reference)
    assert store.answer_cache_size(workspace.id) == 1
    assert store.get_run(completed_run.id).status is JobStatus.COMPLETED
    assert store.get_job(completed_job.id).status is JobStatus.COMPLETED
    store.close()


def test_pinned_runs_and_jobs_survive_confirmed_retention_cleanup(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Pinned history")
    )
    run = store.create_run(
        "run-pinned",
        workspace.id,
        {
            "session_id": "session-pinned",
            "question": "keep this",
            "evidence_mode": "strict",
        },
    )
    store.update_run(run.id, status=JobStatus.COMPLETED, pinned=True)
    job, _ = store.create_job_idempotent(
        job_id="job-pinned",
        workspace_id=workspace.id,
        kind="ingest",
        payload={"source": "keep"},
        idempotency_key="pinned-key",
    )
    store.update_job(job.id, status=JobStatus.COMPLETED, pinned=True)
    reference = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    old = (reference - timedelta(days=60)).isoformat()
    with store._lock:
        store._db.execute("UPDATE runs SET updated_at = ?", (old,))
        store._db.execute("UPDATE jobs SET updated_at = ?", (old,))
        store._db.execute("UPDATE idempotency_keys SET created_at = ?", (old,))

    plan = store.plan_retention_cleanup(workspace.id, now=reference)
    actions = {action.category: action for action in plan.actions}
    assert actions[RetentionCategory.RUNS].eligible_records == 0
    assert actions[RetentionCategory.RUNS].protected_records == 1
    assert actions[RetentionCategory.JOBS].eligible_records == 0
    assert actions[RetentionCategory.JOBS].protected_records == 1
    assert actions[RetentionCategory.IDEMPOTENCY_KEYS].eligible_records == 0
    assert actions[RetentionCategory.IDEMPOTENCY_KEYS].protected_records == 1

    store.purge_retention_cleanup(plan, confirmation="PURGE_EXPIRED", now=reference)
    assert store.get_run(run.id).pinned is True
    assert store.get_job(job.id).pinned is True
    store.close()


def test_exact_answer_cache_enforces_workspace_ttl_on_read(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Cache TTL")
    )
    store.cache_answer(
        cache_key="expired-cache",
        workspace_id=workspace.id,
        index_fingerprint="index",
        config_fingerprint="config",
        request={"question": "private"},
        answer="private answer",
        citations=[],
        max_entries=64,
    )
    expired = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    with store._lock:
        store._db.execute(
            "UPDATE answer_cache SET last_used_at = ? WHERE cache_key = ?",
            (expired, "expired-cache"),
        )

    assert store.cached_answer("expired-cache") is None
    assert store.answer_cache_size(workspace.id) == 0
    store.close()


def test_event_retention_scrubs_content_but_protects_active_streams(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Event TTL")
    )
    completed = store.create_run(
        "run-event-completed",
        workspace.id,
        {
            "session_id": "session-event",
            "question": "private old question",
            "evidence_mode": "strict",
        },
    )
    store.update_run(completed.id, status=JobStatus.COMPLETED)
    active = store.create_run(
        "run-event-active",
        workspace.id,
        {
            "session_id": "session-event",
            "question": "private active question",
            "evidence_mode": "strict",
        },
    )
    old_completed = store.append_event(
        event_type="assistant.delta",
        correlation_id=completed.id,
        workspace_id=workspace.id,
        run_id=completed.id,
        payload={"delta": "PRIVATE COMPLETED ANSWER"},
    )
    old_active = store.append_event(
        event_type="assistant.delta",
        correlation_id=active.id,
        workspace_id=workspace.id,
        run_id=active.id,
        payload={"delta": "PRIVATE ACTIVE ANSWER"},
    )
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    with store._lock:
        store._db.execute(
            "UPDATE events SET timestamp = ? WHERE event_id IN (?, ?)",
            (old, old_completed.event_id, old_active.event_id),
        )

    assert store.compact_expired_event_payloads(workspace.id, force=True) == 1
    events = {item.event_id: item for item in store.events_after(0, workspace_id=workspace.id)}
    assert events[old_completed.event_id].payload == {"retention": "content-expired"}
    assert events[old_active.event_id].payload == {"delta": "PRIVATE ACTIVE ANSWER"}
    store.close()
