from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="omaragd", description="OmaRag backend daemon")
    result.add_argument("--data-dir", type=Path)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--token")
    result.add_argument("--no-auth", action="store_true")
    result.add_argument("--log-level", default="info")
    return result


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides = {
        "host": args.host,
        "port": args.port,
    }
    if args.token is not None:
        overrides["bearer_token"] = args.token
    if args.no_auth:
        overrides["auth_enabled"] = False
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    return Settings(**overrides)


def cli() -> None:
    args = parser().parse_args()
    settings = settings_from_args(args)
    app = create_app(settings)
    services = app.state.services
    if services.token_path:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("omaragd").info(
            "Bearer token stored at %s (mode 0600)", services.token_path
        )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=args.log_level)


if __name__ == "__main__":
    cli()
