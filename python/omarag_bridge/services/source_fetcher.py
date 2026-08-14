from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ..models.errors import ConflictError, UpstreamUnavailableError

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SAFE_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".html", ".htm", ".md", ".txt"})


@dataclass(frozen=True, slots=True)
class DownloadedSource:
    path: Path
    fingerprint: str
    size_bytes: int
    final_reference: str


def _temporary_suffix(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.casefold()
    return suffix if suffix in _SAFE_SUFFIXES else ".bin"


async def download_url_source(
    url: str,
    directory: Path,
    *,
    authorize: Callable[[str], None],
    max_bytes: int = 4 * 1024**3,
    max_redirects: int = 5,
    client: httpx.AsyncClient | None = None,
) -> DownloadedSource:
    """Download an authorized URL into a private, bounded local work file.

    Redirects are deliberately handled one hop at a time so the caller's
    current EgressPolicy sees every destination.  The returned reference is
    content-opaque and safe to persist in public provenance.
    """

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if not 0 <= max_redirects <= 10:
        raise ValueError("max_redirects must be between 0 and 10")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ConflictError("Managed URL-import storage is not trustworthy")
    directory.chmod(0o700)

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": "OmaRag/1.2 URL import"},
        )

    current = url.strip()
    temporary: Path | None = None
    try:
        for hop in range(max_redirects + 1):
            authorize(current)
            try:
                async with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise UpstreamUnavailableError(
                                "URL source returned an invalid redirect"
                            )
                        if hop >= max_redirects:
                            raise UpstreamUnavailableError("URL source exceeded the redirect limit")
                        current = urljoin(str(response.url), location)
                        continue
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            declared_bytes = int(declared)
                        except ValueError as exc:
                            raise UpstreamUnavailableError(
                                "URL source returned an invalid content length"
                            ) from exc
                        if declared_bytes < 0 or declared_bytes > max_bytes:
                            raise ConflictError("URL source exceeds the import size limit")

                    fd, name = tempfile.mkstemp(
                        prefix=".url-import-",
                        suffix=_temporary_suffix(current),
                        dir=directory,
                    )
                    os.close(fd)
                    temporary = Path(name)
                    temporary.chmod(0o600)
                    digest = hashlib.sha256()
                    size = 0
                    with temporary.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise ConflictError("URL source exceeds the import size limit")
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size == 0:
                        raise ConflictError("URL source is empty")
                    fingerprint = digest.hexdigest()
                    return DownloadedSource(
                        path=temporary,
                        fingerprint=fingerprint,
                        size_bytes=size,
                        final_reference=f"omarag://imports/sha256/{fingerprint}",
                    )
            except httpx.HTTPError as exc:
                raise UpstreamUnavailableError("URL source could not be downloaded") from exc
        raise UpstreamUnavailableError("URL source exceeded the redirect limit")
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            await client.aclose()
