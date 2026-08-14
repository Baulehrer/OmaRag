from __future__ import annotations

from pathlib import Path

import yaml

from omarag_bridge.adapters.haiku_v070 import VanillaHaikuAdapter, _absolute_pages
from omarag_bridge.models.domain import EvidenceMode


def _database_with_config(tmp_path: Path, model: dict[str, object]) -> Path:
    workspace = tmp_path / "library.omarag"
    database = workspace / "database" / "knowledge.lancedb"
    database.parent.mkdir(parents=True)
    (workspace / "haiku.rag.yaml").write_text(
        yaml.safe_dump({"qa": {"model": model}}),
        encoding="utf-8",
    )
    return database


def test_request_config_bounds_local_answers_and_disables_hidden_reasoning(
    tmp_path: Path,
) -> None:
    database = _database_with_config(
        tmp_path,
        {
            "provider": "ollama",
            "name": "qwen3.5:4b-q4_K_M",
            "enable_thinking": False,
        },
    )

    config = VanillaHaikuAdapter()._request_config(database, EvidenceMode.STRICT)

    assert config.qa.model.max_tokens == 1024
    assert config.qa.model.extra_body == {"reasoning_effort": "none"}


def test_request_config_preserves_explicit_answer_budget_and_cloud_settings(
    tmp_path: Path,
) -> None:
    database = _database_with_config(
        tmp_path,
        {
            "provider": "openai",
            "name": "example-model",
            "enable_thinking": False,
            "max_tokens": 640,
            "extra_body": {"custom": "kept"},
        },
    )

    config = VanillaHaikuAdapter()._request_config(database, EvidenceMode.NORMAL)

    assert config.qa.model.max_tokens == 640
    assert config.qa.model.extra_body == {"custom": "kept"}


def test_page_hydration_applies_legacy_offset_but_not_book_v2_offset() -> None:
    assert _absolute_pages([1, 2], {"page_offset": 120}) == [121, 122]
    assert _absolute_pages([121, 122], {"page_offset": 120, "page_number_mode": "absolute"}) == [
        121,
        122,
    ]
