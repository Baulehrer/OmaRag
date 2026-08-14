from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from omarag_bridge.models.domain import EgressPayloadClass, PrivacyMode, PrivacyPolicy
from omarag_bridge.models.errors import ConflictError
from omarag_bridge.services.egress_policy import EgressPolicy, EgressPolicyError
from omarag_bridge.services.source_fetcher import download_url_source


@pytest.mark.asyncio
async def test_url_download_reauthorizes_redirect_and_writes_private_file(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def authorize(url: str) -> None:
        seen.append(url)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "source.test":
            return httpx.Response(302, headers={"location": "https://cdn.test/book.pdf"})
        return httpx.Response(200, content=b"%PDF-private-book")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloaded = await download_url_source(
            "https://source.test/start",
            tmp_path / "downloads",
            authorize=authorize,
            client=client,
        )

    assert seen == ["https://source.test/start", "https://cdn.test/book.pdf"]
    assert downloaded.path.read_bytes() == b"%PDF-private-book"
    assert downloaded.path.stat().st_mode & 0o777 == 0o600
    assert downloaded.path.parent.stat().st_mode & 0o777 == 0o700
    content_digest = hashlib.sha256(b"%PDF-private-book").hexdigest()
    assert downloaded.final_reference == f"omarag://imports/sha256/{content_digest}"
    downloaded.path.unlink()


@pytest.mark.asyncio
async def test_url_download_denial_or_stream_limit_leaves_no_partial_file(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "source.test":
            return httpx.Response(302, headers={"location": "https://blocked.test/book.pdf"})
        return httpx.Response(200, content=b"too-large")

    def deny_redirect(url: str) -> None:
        if "blocked" in url:
            raise ConflictError("denied")

    destination = tmp_path / "downloads"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ConflictError, match="denied"):
            await download_url_source(
                "https://source.test/start",
                destination,
                authorize=deny_redirect,
                client=client,
            )
        with pytest.raises(ConflictError, match="size limit"):
            await download_url_source(
                "https://allowed.test/book.pdf",
                destination,
                authorize=lambda _: None,
                max_bytes=4,
                client=client,
            )

    assert list(destination.iterdir()) == []


@pytest.mark.asyncio
async def test_url_download_blocks_a_redirect_to_a_non_public_origin(
    tmp_path: Path,
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "source.test":
            return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
        raise AssertionError("a denied redirect destination must not be requested")

    policy = EgressPolicy(PrivacyPolicy(mode=PrivacyMode.CLOUD_ALLOWED, cloud_acknowledged=True))
    destination = tmp_path / "downloads"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EgressPolicyError):
            await download_url_source(
                "https://source.test/start",
                destination,
                authorize=lambda url: policy.authorize_http(url, EgressPayloadClass.URL_SOURCE),
                client=client,
            )

    assert requested == ["https://source.test/start"]
    assert list(destination.iterdir()) == []
