from __future__ import annotations

import json

import httpx
import pytest

from omarag_bridge.services.ollama_stream import (
    OllamaDigestMismatchError,
    OllamaGenerationOptions,
    OllamaProtocolError,
    OllamaStreamClient,
)


def _models() -> list[dict[str, object]]:
    return [
        {
            "name": "qwen3.5:4b",
            "model": "qwen3.5:4b",
            "digest": "sha256-generator",
            "size": 3_400_000_000,
            "details": {"parameter_size": "4B", "quantization_level": "Q4_K_M"},
            "capabilities": ["completion"],
        },
        {
            "name": "qwen3-embedding:0.6b",
            "digest": "sha256-embedder",
            "size": 639_000_000,
            "details": {"parameter_size": "596M", "quantization_level": "Q8_0"},
            "capabilities": ["embedding"],
        },
    ]


@pytest.mark.asyncio
async def test_model_resolution_readiness_and_digest_are_read_only() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": _models()})
        if request.url.path == "/api/ps":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3.5:4b", "digest": "sha256-generator"},
                        {"name": "qwen3-embedding:0.6b", "digest": "sha256-embedder"},
                    ]
                },
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = OllamaStreamClient(client=http)
        identity = await client.resolve_model("qwen3.5:4b", expected_digest="sha256-generator")
        readiness = await client.check_readiness(
            "qwen3.5:4b",
            expected_digest="sha256-generator",
            required_resident_models=("qwen3-embedding:0.6b",),
        )

    assert identity.digest == "sha256-generator"
    assert identity.parameter_size == "4B"
    assert readiness.ready
    assert readiness.status == "ready"
    assert {request.method for request in requests} == {"GET"}
    assert {request.url.path for request in requests} == {"/api/tags", "/api/ps"}


@pytest.mark.asyncio
async def test_installed_model_inventory_is_short_lived_and_explicitly_invalidated() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.url.path == "/api/tags"
        calls += 1
        return httpx.Response(200, json={"models": _models()})

    base_url = "http://inventory-cache.test"
    OllamaStreamClient.invalidate_inventory(base_url)
    async with httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler)) as http:
        client = OllamaStreamClient(base_url=base_url, client=http)
        # Injected transports normally stay isolated. Enable the shared path
        # explicitly here to exercise the production cache deterministically.
        client._uses_shared_inventory_cache = True
        assert await client.list_models() == await client.list_models()
        assert calls == 1
        OllamaStreamClient.invalidate_inventory(base_url)
        await client.list_models()
        assert calls == 2
    OllamaStreamClient.invalidate_inventory(base_url)


@pytest.mark.asyncio
async def test_readiness_reports_degraded_without_loading_missing_resident_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": _models()})
        return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b"}]})

    async with httpx.AsyncClient(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    ) as http:
        readiness = await OllamaStreamClient(client=http).check_readiness(
            "qwen3.5:4b", required_resident_models=("qwen3-embedding:0.6b",)
        )

    assert not readiness.ready
    assert readiness.status == "latency_degraded"
    assert readiness.missing_resident_models == ("qwen3-embedding:0.6b",)


@pytest.mark.asyncio
async def test_readiness_detects_a_stale_resident_digest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": _models()})
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen3.5:4b", "digest": "stale-generator"},
                    {"name": "qwen3-embedding:0.6b", "digest": "sha256-embedder"},
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    ) as http:
        readiness = await OllamaStreamClient(client=http).check_readiness(
            "qwen3.5:4b",
            required_resident_digests={"qwen3-embedding:0.6b": "sha256-embedder"},
        )

    assert not readiness.ready
    assert readiness.status == "resident_digest_mismatch"
    assert readiness.mismatched_resident_models == ("qwen3.5:4b",)


@pytest.mark.asyncio
async def test_stream_chat_pins_digest_and_yields_native_ndjson_metrics() -> None:
    posted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": _models()})
        if request.url.path == "/api/chat":
            posted.update(json.loads(request.content))
            chunks = [
                {
                    "model": "qwen3.5:4b",
                    "message": {"role": "assistant", "content": "Der "},
                    "done": False,
                },
                {
                    "model": "qwen3.5:4b",
                    "message": {"role": "assistant", "content": "Wert."},
                    "done": False,
                },
                {
                    "model": "qwen3.5:4b",
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "total_duration": 1000,
                    "load_duration": 10,
                    "prompt_eval_count": 100,
                    "prompt_eval_duration": 200,
                    "eval_count": 2,
                    "eval_duration": 300,
                },
            ]
            body = "".join(json.dumps(chunk) + "\n" for chunk in chunks)
            return httpx.Response(
                200, content=body, headers={"content-type": "application/x-ndjson"}
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = OllamaStreamClient(client=http)
        events = [
            event
            async for event in client.stream_chat(
                model="qwen3.5:4b",
                expected_digest="sha256-generator",
                messages=[{"role": "user", "content": "Frage"}],
                options=OllamaGenerationOptions(
                    num_ctx=4096, num_predict=256, temperature=0.0, seed=7
                ),
            )
        ]

    assert "".join(event.content for event in events) == "Der Wert."
    assert all(event.model_digest == "sha256-generator" for event in events)
    assert events[-1].done
    assert events[-1].done_reason == "stop"
    assert events[-1].prompt_eval_count == 100
    assert posted["stream"] is True
    assert posted["think"] is False
    assert posted["keep_alive"] == -1
    assert posted["options"] == {
        "num_ctx": 4096,
        "num_predict": 256,
        "temperature": 0.0,
        "seed": 7,
    }


@pytest.mark.asyncio
async def test_stream_rejects_digest_drift_before_chat_request() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"models": _models()})

    async with httpx.AsyncClient(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = OllamaStreamClient(client=http)
        with pytest.raises(OllamaDigestMismatchError):
            async for _ in client.stream_chat(
                model="qwen3.5:4b",
                expected_digest="old-digest",
                messages=[{"role": "user", "content": "Frage"}],
                options=OllamaGenerationOptions(num_ctx=4096, num_predict=64),
            ):
                pass

    assert paths == ["/api/tags"]


@pytest.mark.asyncio
async def test_stream_rejects_invalid_or_truncated_ndjson() -> None:
    responses = iter(
        [
            '{"model":"qwen3.5:4b","message":{"content":"x"},"done":false}\nnot-json\n',
            '{"model":"qwen3.5:4b","message":{"content":"x"},"done":false}\n',
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": _models()})
        return httpx.Response(200, content=next(responses))

    async with httpx.AsyncClient(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = OllamaStreamClient(client=http)
        with pytest.raises(OllamaProtocolError, match="invalid NDJSON"):
            async for _ in client.stream_chat(
                model="qwen3.5:4b",
                messages=[{"role": "user", "content": "Frage"}],
                options=OllamaGenerationOptions(num_ctx=4096, num_predict=64),
            ):
                pass
        with pytest.raises(OllamaProtocolError, match="without a done"):
            async for _ in client.stream_chat(
                model="qwen3.5:4b",
                messages=[{"role": "user", "content": "Frage"}],
                options=OllamaGenerationOptions(num_ctx=4096, num_predict=64),
            ):
                pass


def test_generation_options_and_message_fields_are_bounded() -> None:
    with pytest.raises(ValueError, match="num_predict"):
        OllamaGenerationOptions(num_ctx=4096, num_predict=0)
