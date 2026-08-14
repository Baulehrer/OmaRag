from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

import httpx

from ..models.book import BookLine, BookPage, NavigationRegion, NavigationRole

_ROLES: tuple[NavigationRole, ...] = (
    "toc",
    "index",
    "glossary",
    "abbreviations",
    "symbols",
    "figures",
    "tables",
    "formulas",
)
_LOCATOR_ROLES = frozenset({"toc", "index", "figures", "tables", "formulas"})
_FRONT_ROLES = frozenset({"toc", "figures", "tables", "formulas"})
_LABEL = r"(?:[a-z]{1,4}[-–]?\d+[a-z]?|\d+[a-z]?|[ivxlcdm]+)"
_LOCATOR_SUFFIX = re.compile(
    rf"(?P<value>{_LABEL}(?:\s*[-–—]\s*{_LABEL})?(?:\s*f{{1,2}}\.?)?"
    rf"(?:\s*,\s*{_LABEL}(?:\s*[-–—]\s*{_LABEL})?(?:\s*f{{1,2}}\.?)?)*)\s*$",
    re.IGNORECASE,
)
_SEPARATOR = re.compile(r"(?:[:–—]\s+|\t+|\s{2,})")


@dataclass(frozen=True, slots=True)
class StructureFallbackLimits:
    """Hard per-book bounds for optional model-assisted routing metadata."""

    max_calls: int = 4
    max_candidates_per_call: int = 64
    max_candidate_chars: int = 320
    max_request_bytes: int = 64 * 1024
    max_response_bytes: int = 32 * 1024
    request_timeout_seconds: float = 20.0
    total_timeout_seconds: float = 60.0
    max_output_tokens: int = 1024

    def __post_init__(self) -> None:
        if not 1 <= self.max_calls <= 4:
            raise ValueError("structure fallback permits at most four calls per book")
        if not 4 <= self.max_candidates_per_call <= 96:
            raise ValueError("candidate bound is outside the safe range")
        if not 80 <= self.max_candidate_chars <= 512:
            raise ValueError("candidate text bound is outside the safe range")
        if not 8 * 1024 <= self.max_request_bytes <= 128 * 1024:
            raise ValueError("request byte bound is outside the safe range")
        if not 1024 <= self.max_response_bytes <= 64 * 1024:
            raise ValueError("response byte bound is outside the safe range")
        if not 1.0 <= self.request_timeout_seconds <= 30.0:
            raise ValueError("request timeout is outside the safe range")
        if not self.request_timeout_seconds <= self.total_timeout_seconds <= 120.0:
            raise ValueError("total timeout is outside the safe range")
        if not 128 <= self.max_output_tokens <= 2048:
            raise ValueError("output token bound is outside the safe range")


DEFAULT_STRUCTURE_FALLBACK_LIMITS = StructureFallbackLimits()


@dataclass(frozen=True, slots=True)
class StructureFallbackCandidate:
    candidate_id: str
    page_no: int
    source_ref: str | None
    text: str
    substrings: tuple[str, ...]
    locators: tuple[str, ...]
    ordinal: int


@dataclass(frozen=True, slots=True)
class StructureRouteSelection:
    """Validated model routing metadata; never factual evidence."""

    candidate_id: str
    page_no: int
    source_ref: str | None
    substring: str
    locator: str | None
    role: NavigationRole
    level: int
    parent_id: str | None
    objective: float


@dataclass(frozen=True, slots=True)
class StructureFallbackRequest:
    """Transport-neutral request for an injected, already-installed local model."""

    endpoint: str
    model: str
    system: str
    payload: dict[str, Any]
    json_schema: dict[str, Any]
    temperature: Literal[0] = 0
    seed: Literal[0] = 0
    max_output_tokens: int = 1024
    timeout_seconds: float = 20.0
    allow_download: Literal[False] = False
    expected_digest: str | None = None


class StructureFallbackRunner(Protocol):
    """Injected local inference boundary.

    Implementations may call only the supplied loopback/Unix endpoint and an
    already-installed model. They must disable environment proxies and must not
    resolve, pull, or download models.
    """

    async def run(self, request: StructureFallbackRequest) -> Mapping[str, Any] | str: ...


@dataclass(frozen=True, slots=True)
class OllamaStructureFallbackRunner:
    """Local-only Ollama runner with no pull or model-resolution side effects."""

    transport: httpx.AsyncBaseTransport | None = None

    async def run(self, request: StructureFallbackRequest) -> Mapping[str, Any] | str:
        if not is_local_structure_endpoint(request.endpoint):
            raise _FallbackRejected("endpoint-not-local")
        if not _valid_model(request.model):
            raise _FallbackRejected("model-invalid")
        timeout = httpx.Timeout(
            request.timeout_seconds,
            connect=min(2.0, request.timeout_seconds),
            pool=min(2.0, request.timeout_seconds),
        )
        async with httpx.AsyncClient(
            base_url=request.endpoint.rstrip("/"),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        ) as client:
            tags = await _bounded_json_response(client, "GET", "/api/tags", None, 256 * 1024)
            installed = [
                item
                for item in tags.get("models", [])
                if isinstance(item, dict)
                and _same_model(str(item.get("name") or item.get("model") or ""), request.model)
                and not item.get("remote_host")
                and not item.get("remote_model")
            ]
            if len(installed) != 1:
                raise _FallbackRejected("model-not-installed-locally")
            installed_digest = str(installed[0].get("digest") or "").removeprefix("sha256:")
            if not installed_digest:
                raise _FallbackRejected("model-digest-missing")
            if request.expected_digest == "":
                raise _FallbackRejected("pinned-model-digest-missing")
            expected_digest = (
                request.expected_digest.removeprefix("sha256:")
                if request.expected_digest is not None
                else None
            )
            if expected_digest is not None and (
                not installed_digest
                or not hmac.compare_digest(expected_digest.casefold(), installed_digest.casefold())
            ):
                raise _FallbackRejected("model-digest-mismatch")
            body = {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {
                        "role": "user",
                        "content": json.dumps(
                            request.payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
                "stream": False,
                "format": request.json_schema,
                "think": False,
                "keep_alive": "30s",
                "options": {
                    "temperature": request.temperature,
                    "seed": request.seed,
                    "num_predict": request.max_output_tokens,
                },
            }
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded) > DEFAULT_STRUCTURE_FALLBACK_LIMITS.max_request_bytes:
                raise _FallbackRejected("request-too-large")
            response = await _bounded_json_response(
                client,
                "POST",
                "/api/chat",
                body,
                DEFAULT_STRUCTURE_FALLBACK_LIMITS.max_response_bytes,
            )
            final_tags = await _bounded_json_response(client, "GET", "/api/tags", None, 256 * 1024)
            final_digests = [
                str(item.get("digest") or "").removeprefix("sha256:")
                for item in final_tags.get("models", [])
                if isinstance(item, dict)
                and _same_model(str(item.get("name") or item.get("model") or ""), request.model)
                and not item.get("remote_host")
                and not item.get("remote_model")
            ]
            if len(final_digests) != 1 or not hmac.compare_digest(
                installed_digest.casefold(), final_digests[0].casefold()
            ):
                raise _FallbackRejected("model-mutated-during-call")
        message = response.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise _FallbackRejected("response-content-missing")
        return content


def _same_model(left: str, right: str) -> bool:
    return left.removesuffix(":latest") == right.removesuffix(":latest")


async def _bounded_json_response(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    max_bytes: int,
) -> dict[str, Any]:
    chunks: list[bytes] = []
    size = 0
    async with client.stream(method, path, json=payload) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise _FallbackRejected("response-too-large")
            chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _FallbackRejected("response-invalid-json") from exc
    if not isinstance(value, dict):
        raise _FallbackRejected("response-invalid-shape")
    return value


@dataclass(frozen=True, slots=True)
class StructureFallbackResult:
    regions: list[NavigationRegion]
    selections: list[StructureRouteSelection] = field(default_factory=list)
    candidate_regions: int = 0
    calls: int = 0
    applied_regions: int = 0
    skipped_regions: int = 0
    failures: tuple[str, ...] = ()
    model: str | None = None

    @property
    def used(self) -> bool:
        return self.applied_regions > 0


class _FallbackRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_local_structure_endpoint(value: str | None) -> bool:
    """Accept only explicit loopback HTTP origins or absolute Unix sockets."""

    if not value or len(value) > 4096 or any(ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        if scheme == "unix":
            decoded = unquote(parsed.path)
            path = Path(decoded)
            return bool(
                not parsed.netloc
                and not parsed.query
                and not parsed.fragment
                and path.is_absolute()
                and ".." not in path.parts
                and decoded not in {"", "/"}
                and "\x00" not in decoded
            )
        if (
            scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            return False
        host = parsed.hostname.rstrip(".").casefold()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return False
        if host == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError, UnicodeError):
        return False


def _valid_model(value: str | None) -> bool:
    return bool(
        value
        and len(value) <= 256
        and "://" not in value
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _stable_candidate_id(line: BookLine, ordinal: int) -> str:
    material = f"{line.page_no}\0{line.source_ref or ''}\0{ordinal}\0{line.text}".encode(
        "utf-8", errors="replace"
    )
    return f"route-{hashlib.sha256(material).hexdigest()[:24]}"


def _candidate_parts(text: str, *, max_chars: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    stripped = text.strip()
    bounded = stripped[:max_chars].rstrip()
    substrings = [bounded] if bounded else []
    locators: list[str] = []
    match = _LOCATOR_SUFFIX.search(stripped)
    if match is not None:
        raw = match.group("value")
        locators.append(raw)
        locators.extend(part.strip() for part in raw.split(",") if part.strip())
        title = stripped[: match.start()].rstrip(" .,:;\t")
        if title:
            substrings.append(title[:max_chars].rstrip())
    separator = _SEPARATOR.search(stripped)
    if separator is not None:
        term = stripped[: separator.start()].strip(" .,:;\t")
        if term:
            substrings.append(term[:max_chars].rstrip())
    return tuple(dict.fromkeys(substrings)), tuple(dict.fromkeys(locators))


def _region_candidates(
    pages: Sequence[BookPage],
    region: NavigationRegion,
    *,
    limits: StructureFallbackLimits,
) -> list[StructureFallbackCandidate]:
    lines = [
        line
        for page in sorted(pages, key=lambda item: item.page_no)
        if region.page_start <= page.page_no <= region.page_end
        for line in page.lines
        if line.text.strip()
    ]
    candidates: list[StructureFallbackCandidate] = []
    for ordinal, line in enumerate(lines):
        substrings, locators = _candidate_parts(line.text, max_chars=limits.max_candidate_chars)
        if not substrings:
            continue
        candidates.append(
            StructureFallbackCandidate(
                candidate_id=_stable_candidate_id(line, ordinal),
                page_no=line.page_no,
                source_ref=line.source_ref,
                text=line.text,
                substrings=substrings,
                locators=locators,
                ordinal=ordinal,
            )
        )
    candidates.sort(
        key=lambda item: (
            not bool(item.locators),
            item.page_no,
            item.ordinal,
            item.candidate_id,
        )
    )
    return candidates[: limits.max_candidates_per_call]


def _response_schema(candidates: Sequence[StructureFallbackCandidate]) -> dict[str, Any]:
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    substrings = list(
        dict.fromkeys(value for candidate in candidates for value in candidate.substrings)
    )
    locators = list(
        dict.fromkeys(value for candidate in candidates for value in candidate.locators)
    )
    locator_schema: dict[str, Any] = {"type": "null"}
    if locators:
        locator_schema = {
            "anyOf": [
                {"type": "string", "enum": locators},
                {"type": "null"},
            ]
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["selections"],
        "properties": {
            "selections": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(candidates),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "candidate_id",
                        "substring",
                        "locator",
                        "role",
                        "level",
                        "parent_id",
                    ],
                    "properties": {
                        "candidate_id": {"type": "string", "enum": candidate_ids},
                        "substring": {"type": "string", "enum": substrings},
                        "locator": locator_schema,
                        "role": {"type": "string", "enum": list(_ROLES)},
                        "level": {"type": "integer", "minimum": 0, "maximum": 6},
                        "parent_id": {
                            "anyOf": [
                                {"type": "string", "enum": candidate_ids},
                                {"type": "null"},
                            ]
                        },
                    },
                },
            }
        },
    }


def _request_for_region(
    *,
    endpoint: str,
    model: str,
    region: NavigationRegion,
    candidates: Sequence[StructureFallbackCandidate],
    limits: StructureFallbackLimits,
    expected_digest: str | None,
) -> StructureFallbackRequest:
    payload = {
        "region": {
            "current_role": region.role,
            "page_start": region.page_start,
            "page_end": region.page_end,
            "deterministic_score": region.score,
        },
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "page_no": candidate.page_no,
                "text": candidate.text[: limits.max_candidate_chars],
                "allowed_substrings": list(candidate.substrings),
                "allowed_locators": list(candidate.locators),
            }
            for candidate in candidates
        ],
    }
    schema = _response_schema(candidates)
    encoded = json.dumps(
        {"payload": payload, "schema": schema},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > limits.max_request_bytes:
        raise _FallbackRejected("request-too-large")
    return StructureFallbackRequest(
        endpoint=endpoint,
        model=model,
        system=(
            "Du klassifizierst ausschließlich vorhandene Buch-Routingkandidaten. "
            "Wähle nur IDs, exakte Substrings und Locators aus der Eingabe. "
            "Erfinde oder ändere niemals Text, Seite oder Locator. Setze nur Rolle, "
            "Hierarchieebene 0 bis 6 und eine Eltern-ID aus der Kandidatenliste. "
            "Antworte ausschließlich im JSON-Schema."
        ),
        payload=payload,
        json_schema=schema,
        max_output_tokens=limits.max_output_tokens,
        timeout_seconds=limits.request_timeout_seconds,
        expected_digest=expected_digest,
    )


def _decode_response(value: Mapping[str, Any] | str, *, max_bytes: int) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > max_bytes:
            raise _FallbackRejected("response-too-large")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _FallbackRejected("invalid-json") from exc
    elif isinstance(value, Mapping):
        decoded = dict(value)
        try:
            encoded = json.dumps(decoded, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise _FallbackRejected("invalid-json") from exc
        if len(encoded) > max_bytes:
            raise _FallbackRejected("response-too-large")
    else:
        raise _FallbackRejected("invalid-response-type")
    if not isinstance(decoded, dict) or set(decoded) != {"selections"}:
        raise _FallbackRejected("schema-violation")
    return decoded


def _validated_selections(
    response: dict[str, Any],
    candidates: Sequence[StructureFallbackCandidate],
) -> tuple[list[tuple[StructureFallbackCandidate, dict[str, Any]]], NavigationRole]:
    raw_selections = response.get("selections")
    if not isinstance(raw_selections, list) or not raw_selections:
        raise _FallbackRejected("schema-violation")
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    selected: list[tuple[StructureFallbackCandidate, dict[str, Any]]] = []
    seen: set[str] = set()
    required = {"candidate_id", "substring", "locator", "role", "level", "parent_id"}
    for raw in raw_selections:
        if not isinstance(raw, dict) or set(raw) != required:
            raise _FallbackRejected("schema-violation")
        candidate_id = raw.get("candidate_id")
        candidate = by_id.get(candidate_id) if isinstance(candidate_id, str) else None
        if candidate is None or candidate.candidate_id in seen:
            raise _FallbackRejected("unknown-or-duplicate-candidate")
        substring = raw.get("substring")
        locator = raw.get("locator")
        role = raw.get("role")
        level = raw.get("level")
        parent_id = raw.get("parent_id")
        if (
            not isinstance(substring, str)
            or substring not in candidate.substrings
            or substring not in candidate.text
            or (locator is not None and locator not in candidate.locators)
            or role not in _ROLES
            or type(level) is not int
            or not 0 <= level <= 6
            or (parent_id is not None and not isinstance(parent_id, str))
        ):
            raise _FallbackRejected("candidate-mutation")
        seen.add(candidate.candidate_id)
        selected.append((candidate, raw))
    roles = {raw["role"] for _candidate, raw in selected}
    if len(roles) != 1:
        raise _FallbackRejected("mixed-region-roles")
    role = next(iter(roles))

    selected_by_id = {candidate.candidate_id: raw for candidate, raw in selected}
    candidate_order = {candidate.candidate_id: candidate.ordinal for candidate, _raw in selected}
    for candidate, raw in selected:
        level = raw["level"]
        parent_id = raw["parent_id"]
        if level == 0:
            if parent_id is not None:
                raise _FallbackRejected("invalid-parent")
            continue
        parent = selected_by_id.get(parent_id)
        if (
            parent is None
            or parent_id == candidate.candidate_id
            or candidate_order[parent_id] >= candidate.ordinal
            or parent["level"] != level - 1
        ):
            raise _FallbackRejected("invalid-parent")

    for candidate, _raw in selected:
        visited: set[str] = set()
        current: str | None = candidate.candidate_id
        while current is not None:
            if current in visited:
                raise _FallbackRejected("hierarchy-cycle")
            visited.add(current)
            parent = selected_by_id.get(current)
            current = parent["parent_id"] if parent is not None else None
            if len(visited) > 7:
                raise _FallbackRejected("hierarchy-too-deep")
    return selected, role  # type: ignore[return-value]


def _objective(
    *,
    region: NavigationRegion,
    role: NavigationRole,
    selected: Sequence[tuple[StructureFallbackCandidate, dict[str, Any]]],
    candidates: Sequence[StructureFallbackCandidate],
    total_pages: int,
) -> float:
    def matches_role(candidate: StructureFallbackCandidate, locator: str | None) -> bool:
        if role in _LOCATOR_ROLES:
            if locator is None:
                return False
            prefix = candidate.text[: candidate.text.rfind(locator)]
            if role == "index":
                return "," in prefix or bool(re.search(r"\s{2,}|\t", prefix))
            return bool(re.search(r"\.{2,}|\s{2,}|\t", prefix))
        return bool(_SEPARATOR.search(candidate.text))

    minimum = 3 if role in _LOCATOR_ROLES else 2
    format_score = sum(
        matches_role(candidate, raw["locator"]) for candidate, raw in selected
    ) / len(selected)
    if role in _LOCATOR_ROLES:
        plausible = sum(bool(candidate.locators) for candidate in candidates)
    else:
        plausible = sum(bool(_SEPARATOR.search(candidate.text)) for candidate in candidates)
    volume = min(1.0, len(selected) / minimum)
    coverage_target = max(minimum, min(8, plausible or len(candidates)))
    coverage = min(1.0, len(selected) / coverage_target)
    if role in _FRONT_ROLES:
        expected = region.page_start <= max(12, math.ceil(total_pages * 0.2))
    else:
        expected = region.page_end >= max(1, math.floor(total_pages * 0.6))
    position = 1.0 if expected else 0.5
    objective = (
        0.35 * format_score
        + 0.20 * volume
        + 0.15 * coverage
        + 0.15  # hierarchy is already a hard validation gate
        + 0.10 * position
        + 0.05 * float(role == region.role)
    )
    return round(objective, 6)


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, _FallbackRejected):
        return exc.code
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return "timeout"
    return f"runner-{type(exc).__name__.casefold()}"


async def refine_uncertain_navigation_regions(
    *,
    pages: Sequence[BookPage],
    regions: Sequence[NavigationRegion],
    total_pages: int,
    endpoint: str | None,
    model: str | None,
    runner: StructureFallbackRunner | None,
    expected_digest: str | None = None,
    limits: StructureFallbackLimits = DEFAULT_STRUCTURE_FALLBACK_LIMITS,
    inference_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> StructureFallbackResult:
    """Apply bounded local model proposals only when deterministic routing improves.

    Accepted output can change navigation routing metadata and TOC hierarchy,
    but never source text, page numbers, evidence, or locators. Any transport,
    schema, validation, timeout, or objective failure returns the deterministic
    regions unchanged and therefore cannot block book indexing.
    """

    originals = list(regions)
    eligible = [region for region in originals if not region.accepted and region.score < 0.82]
    eligible.sort(key=lambda item: (-item.score, item.page_start, item.role))
    if not eligible:
        return StructureFallbackResult(regions=originals)
    if runner is None:
        return StructureFallbackResult(
            regions=originals,
            candidate_regions=len(eligible),
            skipped_regions=len(eligible),
            failures=("runner-not-configured",),
        )
    if not is_local_structure_endpoint(endpoint):
        return StructureFallbackResult(
            regions=originals,
            candidate_regions=len(eligible),
            skipped_regions=len(eligible),
            failures=("endpoint-not-local",),
            model=model,
        )
    if not _valid_model(model):
        return StructureFallbackResult(
            regions=originals,
            candidate_regions=len(eligible),
            skipped_regions=len(eligible),
            failures=("model-not-configured",),
            model=model,
        )
    assert endpoint is not None and model is not None

    chosen = eligible[: limits.max_calls]
    failures: list[str] = []
    calls = 0
    replacements: dict[tuple[str, int, int], NavigationRegion] = {}
    route_selections: list[StructureRouteSelection] = []
    started = time.monotonic()
    for region in chosen:
        remaining = limits.total_timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            failures.append("total-timeout")
            break
        candidates = _region_candidates(pages, region, limits=limits)
        if not candidates:
            failures.append("no-candidates")
            continue
        try:
            request = _request_for_region(
                endpoint=endpoint,
                model=model,
                region=region,
                candidates=candidates,
                limits=limits,
                expected_digest=expected_digest,
            )
            calls += 1
            timeout = min(limits.request_timeout_seconds, remaining)
            async with asyncio.timeout(timeout):
                if inference_guard is None:
                    raw_response = await runner.run(request)
                else:
                    async with inference_guard():
                        raw_response = await runner.run(request)
            response = _decode_response(raw_response, max_bytes=limits.max_response_bytes)
            selected, role = _validated_selections(response, candidates)
            objective = _objective(
                region=region,
                role=role,
                selected=selected,
                candidates=candidates,
                total_pages=total_pages,
            )
            gain = round(objective - region.score, 6)
            if objective < 0.75 or gain < 0.05:
                raise _FallbackRejected("objective-not-improved")
            source_refs = list(
                dict.fromkeys(
                    candidate.source_ref
                    for candidate, _raw in selected
                    if candidate.source_ref is not None
                )
            )
            replacement = region.model_copy(
                update={
                    "role": role,
                    "score": objective,
                    "accepted": True,
                    "source_refs": source_refs,
                    "metrics": {
                        **region.metrics,
                        "llm_objective_baseline": region.score,
                        "llm_objective": objective,
                        "llm_objective_gain": gain,
                        "llm_selected_candidates": float(len(selected)),
                    },
                }
            )
            key = (region.role, region.page_start, region.page_end)
            replacements[key] = replacement
            route_selections.extend(
                StructureRouteSelection(
                    candidate_id=candidate.candidate_id,
                    page_no=candidate.page_no,
                    source_ref=candidate.source_ref,
                    substring=str(raw["substring"]),
                    locator=str(raw["locator"]) if raw["locator"] is not None else None,
                    role=role,
                    level=int(raw["level"]),
                    parent_id=str(raw["parent_id"]) if raw["parent_id"] is not None else None,
                    objective=objective,
                )
                for candidate, raw in selected
            )
        except Exception as exc:
            failures.append(_failure_code(exc))

    refined = [
        replacements.get((region.role, region.page_start, region.page_end), region)
        for region in originals
    ]
    return StructureFallbackResult(
        regions=refined,
        selections=route_selections,
        candidate_regions=len(eligible),
        calls=calls,
        applied_regions=len(replacements),
        skipped_regions=max(0, len(eligible) - calls),
        failures=tuple(failures),
        model=model,
    )
