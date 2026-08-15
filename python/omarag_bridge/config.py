from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMARAG_", extra="ignore")

    data_dir: Path = Field(default=Path("~/.local/share/omarag").expanduser())
    host: str = "127.0.0.1"
    port: int = 8765
    bearer_token: str | None = None
    auth_enabled: bool = True
    backend_id: str = "local"
    event_poll_seconds: float = 0.5
    event_keepalive_seconds: float = 15.0
    ollama_url: str = "http://127.0.0.1:11434"
    hugging_face_url: str = "https://huggingface.co"
    model_catalog_scan_limit: int = 1000
    model_upload_max_bytes: int = Field(default=64 * 1024**3, ge=1024**2)
    api_memory_high_mb: int = Field(default=384, ge=128)
    api_memory_max_mb: int = Field(default=768, ge=256)
    api_swap_max_mb: int = Field(default=0, ge=0)
    api_tasks_max: int = Field(default=64, ge=16, le=256)
    unload_ollama_models_on_worker_exit: bool = True
    # Upper bound; the resource coordinator grows hot-query residency from 30s
    # to at most 5m and drops it to zero under memory pressure. An explicit env
    # override may choose a lower ceiling.
    worker_query_idle_seconds: float = Field(default=300.0, ge=0.0, le=600.0)
    worker_import_memory_high_mb: int = Field(default=7168, ge=512)
    worker_import_memory_max_mb: int = Field(default=9216, ge=768)
    worker_import_swap_max_mb: int = Field(default=1024, ge=0)
    worker_query_memory_high_mb: int = Field(default=2048, ge=256)
    worker_query_memory_max_mb: int = Field(default=3584, ge=512)
    worker_query_swap_max_mb: int = Field(default=512, ge=0)
    worker_utility_memory_high_mb: int = Field(default=1024, ge=128)
    worker_utility_memory_max_mb: int = Field(default=2048, ge=256)
    worker_utility_swap_max_mb: int = Field(default=256, ge=0)
    worker_tasks_max: int = Field(default=96, ge=16, le=512)
    answer_cache_max_entries: int = Field(default=64, ge=16, le=4096)
    answer_cache_max_bytes: int = Field(default=128 * 1024**2, ge=1024**2)
    retention_sweep_seconds: float = Field(default=3600.0, ge=1.0, le=86400.0)

    @property
    def state_database(self) -> Path:
        return self.data_dir / "omarag.sqlite3"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"
