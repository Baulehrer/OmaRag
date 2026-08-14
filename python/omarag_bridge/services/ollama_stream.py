from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx


class OllamaStreamError(RuntimeError):
    pass


class OllamaModelNotFoundError(OllamaStreamError):
    pass


class OllamaDigestMismatchError(OllamaStreamError):
    pass


class OllamaProtocolError(OllamaStreamError):
    pass


@dataclass(frozen=True, slots=True)
class OllamaModelIdentity:
    name: str
    digest: str
    size: int
    capabilities: tuple[str, ...] = ()
    parameter_size: str | None = None
    quantization: str | None = None


@dataclass(frozen=True, slots=True)
class OllamaResidentModel:
    name: str
    digest: str | None
    size: int
    size_vram: int
    context_length: int | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class OllamaReadiness:
    ready: bool
    status: str
    identity: OllamaModelIdentity | None
    resident_models: tuple[str, ...]
    missing_resident_models: tuple[str, ...] = ()
    mismatched_resident_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OllamaGenerationOptions:
    num_ctx: int
    num_predict: int
    temperature: float = 0.0
    seed: int | None = None

    def __post_init__(self) -> None:
        if not 512 <= self.num_ctx <= 131_072:
            raise ValueError("num_ctx must be between 512 and 131072")
        if not 1 <= self.num_predict <= 4_096:
            raise ValueError("num_predict must be between 1 and 4096")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2")

    def as_payload(self) -> dict[str, int | float]:
        payload: dict[str, int | float] = {
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
            "temperature": self.temperature,
        }
        if self.seed is not None:
            payload["seed"] = self.seed
        return payload


@dataclass(frozen=True, slots=True)
class OllamaStreamEvent:
    model: str
    model_digest: str
    content: str = ""
    thinking: str = ""
    done: bool = False
    done_reason: str | None = None
    total_duration_ns: int | None = None
    load_duration_ns: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration_ns: int | None = None
    eval_count: int | None = None
    eval_duration_ns: int | None = None


@dataclass(slots=True)
class OllamaStreamClient:
    base_url: str = "http://127.0.0.1:11434"
    timeout: httpx.Timeout = field(
        default_factory=lambda: httpx.Timeout(connect=2.0, read=None, write=10.0, pool=2.0)
    )
    client: httpx.AsyncClient | None = None
    inventory_timeout_seconds: float = 5.0
    _owns_client: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.client is None:
            self.client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
            self._owns_client = True

    async def aclose(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()

    async def __aenter__(self) -> OllamaStreamClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def list_models(self) -> tuple[OllamaModelIdentity, ...]:
        response = await self._request("GET", "/api/tags")
        payload = _json_object(response)
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise OllamaProtocolError("Ollama /api/tags returned an invalid models field")
        result: list[OllamaModelIdentity] = []
        for raw in models:
            if not isinstance(raw, dict):
                raise OllamaProtocolError("Ollama /api/tags returned an invalid model")
            name = raw.get("name") or raw.get("model")
            digest = raw.get("digest")
            if not isinstance(name, str) or not isinstance(digest, str) or not digest:
                raise OllamaProtocolError("Ollama model identity lacks name or digest")
            details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
            capabilities = raw.get("capabilities") or []
            if not isinstance(capabilities, list):
                raise OllamaProtocolError("Ollama model capabilities must be a list")
            result.append(
                OllamaModelIdentity(
                    name=name,
                    digest=digest,
                    size=_optional_int(raw.get("size")) or 0,
                    capabilities=tuple(str(item) for item in capabilities),
                    parameter_size=_optional_str(details.get("parameter_size")),
                    quantization=_optional_str(details.get("quantization_level")),
                )
            )
        return tuple(result)

    async def resolve_model(
        self, model: str, *, expected_digest: str | None = None
    ) -> OllamaModelIdentity:
        models = await self.list_models()
        matches = [item for item in models if _same_model_name(item.name, model)]
        if not matches:
            raise OllamaModelNotFoundError(f"Ollama model is not installed: {model}")
        if len(matches) > 1:
            exact = [item for item in matches if item.name == model]
            if len(exact) != 1:
                raise OllamaProtocolError(f"Ollama model name is ambiguous: {model}")
            identity = exact[0]
        else:
            identity = matches[0]
        if expected_digest is not None and identity.digest != expected_digest:
            raise OllamaDigestMismatchError(
                f"Ollama digest changed for {model}: expected {expected_digest}, "
                f"got {identity.digest}"
            )
        return identity

    async def running_models(self) -> tuple[OllamaResidentModel, ...]:
        response = await self._request("GET", "/api/ps")
        payload = _json_object(response)
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise OllamaProtocolError("Ollama /api/ps returned an invalid models field")
        result: list[OllamaResidentModel] = []
        for raw in models:
            if not isinstance(raw, dict):
                raise OllamaProtocolError("Ollama /api/ps returned an invalid model")
            name = raw.get("name") or raw.get("model")
            if not isinstance(name, str):
                raise OllamaProtocolError("resident Ollama model lacks a name")
            result.append(
                OllamaResidentModel(
                    name=name,
                    digest=_optional_str(raw.get("digest")),
                    size=_optional_int(raw.get("size")) or 0,
                    size_vram=_optional_int(raw.get("size_vram")) or 0,
                    context_length=_optional_int(raw.get("context_length")),
                    expires_at=_optional_str(raw.get("expires_at")),
                )
            )
        return tuple(result)

    async def check_readiness(
        self,
        model: str,
        *,
        expected_digest: str | None = None,
        required_resident_models: Sequence[str] = (),
        required_resident_digests: Mapping[str, str] | None = None,
    ) -> OllamaReadiness:
        try:
            identity = await self.resolve_model(model, expected_digest=expected_digest)
        except OllamaModelNotFoundError:
            return OllamaReadiness(False, "model_missing", None, ())
        except OllamaDigestMismatchError:
            return OllamaReadiness(False, "digest_mismatch", None, ())
        except OllamaStreamError:
            return OllamaReadiness(False, "unavailable", None, ())

        try:
            residents = await self.running_models()
        except OllamaStreamError:
            return OllamaReadiness(False, "unavailable", identity, ())
        resident_names = tuple(item.name for item in residents)
        digest_requirements = dict(required_resident_digests or {})
        digest_requirements.setdefault(model, identity.digest)
        required = tuple(dict.fromkeys((model, *required_resident_models, *digest_requirements)))
        missing = tuple(
            name
            for name in required
            if not any(_same_model_name(resident.name, name) for resident in residents)
        )
        mismatched = tuple(
            name
            for name, digest in digest_requirements.items()
            if any(
                _same_model_name(resident.name, name)
                and resident.digest is not None
                and resident.digest != digest
                for resident in residents
            )
        )
        status = (
            "resident_digest_mismatch"
            if mismatched
            else "ready"
            if not missing
            else "latency_degraded"
        )
        return OllamaReadiness(
            not missing and not mismatched,
            status,
            identity,
            resident_names,
            missing,
            mismatched,
        )

    async def stream_chat(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        options: OllamaGenerationOptions,
        expected_digest: str | None = None,
        resolved_identity: OllamaModelIdentity | None = None,
        think: bool | str = False,
        keep_alive: str | int = -1,
    ) -> AsyncIterator[OllamaStreamEvent]:
        if resolved_identity is None:
            identity = await self.resolve_model(model, expected_digest=expected_digest)
        else:
            if not _same_model_name(resolved_identity.name, model):
                raise OllamaProtocolError("resolved model identity does not match the request")
            if expected_digest is not None and resolved_identity.digest != expected_digest:
                raise OllamaDigestMismatchError(
                    f"Ollama digest changed for {model}: expected {expected_digest}, "
                    f"got {resolved_identity.digest}"
                )
            identity = resolved_identity
        if not messages:
            raise ValueError("messages must not be empty")
        if isinstance(think, str) and think not in {"low", "medium", "high", "max"}:
            raise ValueError("think must be boolean or a supported reasoning level")
        request_messages = [_validated_message(item) for item in messages]
        payload = {
            "model": identity.name,
            "messages": request_messages,
            "stream": True,
            "think": think,
            "keep_alive": keep_alive,
            "options": options.as_payload(),
        }
        assert self.client is not None
        saw_done = False
        try:
            async with self.client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise OllamaProtocolError("Ollama emitted invalid NDJSON") from exc
                    event = _stream_event(raw, identity)
                    if saw_done:
                        raise OllamaProtocolError("Ollama emitted data after its done frame")
                    saw_done = event.done
                    yield event
        except httpx.HTTPError as exc:
            raise OllamaStreamError(f"Ollama chat failed: {exc}") from exc
        if not saw_done:
            raise OllamaProtocolError("Ollama stream ended without a done frame")

    async def _request(self, method: str, path: str) -> httpx.Response:
        assert self.client is not None
        try:
            response = await self.client.request(
                method, path, timeout=self.inventory_timeout_seconds
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise OllamaStreamError(f"Ollama request failed: {exc}") from exc


def _stream_event(raw: Any, identity: OllamaModelIdentity) -> OllamaStreamEvent:
    if not isinstance(raw, dict):
        raise OllamaProtocolError("Ollama stream item must be an object")
    model = raw.get("model")
    if not isinstance(model, str) or not _same_model_name(model, identity.name):
        raise OllamaProtocolError("Ollama stream changed the selected model")
    message = raw.get("message") or {}
    if not isinstance(message, dict):
        raise OllamaProtocolError("Ollama stream message must be an object")
    done = raw.get("done", False)
    if not isinstance(done, bool):
        raise OllamaProtocolError("Ollama done field must be boolean")
    return OllamaStreamEvent(
        model=model,
        model_digest=identity.digest,
        content=str(message.get("content") or ""),
        thinking=str(message.get("thinking") or ""),
        done=done,
        done_reason=_optional_str(raw.get("done_reason")),
        total_duration_ns=_optional_int(raw.get("total_duration")),
        load_duration_ns=_optional_int(raw.get("load_duration")),
        prompt_eval_count=_optional_int(raw.get("prompt_eval_count")),
        prompt_eval_duration_ns=_optional_int(raw.get("prompt_eval_duration")),
        eval_count=_optional_int(raw.get("eval_count")),
        eval_duration_ns=_optional_int(raw.get("eval_duration")),
    )


def _validated_message(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"role", "content", "images", "tool_name", "tool_calls"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unsupported Ollama message fields: {sorted(unknown)}")
    role = raw.get("role")
    content = raw.get("content")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError("invalid Ollama message role")
    if not isinstance(content, str):
        raise ValueError("Ollama message content must be text")
    return dict(raw)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise OllamaProtocolError("Ollama returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OllamaProtocolError("Ollama JSON response must be an object")
    return payload


def _same_model_name(left: str, right: str) -> bool:
    return left == right or left == f"{right}:latest" or right == f"{left}:latest"


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) else None
