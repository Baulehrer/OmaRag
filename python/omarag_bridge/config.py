from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMARAG_", extra="ignore")

    data_dir: Path = Field(default=Path("~/.local/share/oracle-of-daedalus").expanduser())
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

    @property
    def state_database(self) -> Path:
        return self.data_dir / "omarag.sqlite3"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"
