from __future__ import annotations

import httpx
import pytest

from omarag_bridge.mcp_server import OmaRagApi, mcp


def test_mcp_api_uses_only_the_daemon_contract() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/v1/workspaces":
            return httpx.Response(200, json=[{"id": "ws-test"}])
        return httpx.Response(404, json={"error": {"message": "fehlt"}})

    api = OmaRagApi(base_url="http://test", transport=httpx.MockTransport(handler))
    assert api.request("GET", "/v1/workspaces") == [{"id": "ws-test"}]
    assert requests == [("GET", "/v1/workspaces")]


def test_mcp_api_ignores_environment_and_does_not_follow_redirects() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/workspaces":
            return httpx.Response(
                302,
                headers={"Location": "http://attacker.invalid/redirected"},
                json={"redirect": "blocked"},
            )
        raise AssertionError(f"unexpected redirected request: {request.url}")

    api = OmaRagApi(base_url="http://test", transport=httpx.MockTransport(handler))

    assert api.client._trust_env is False
    assert api.client.follow_redirects is False
    with pytest.raises(RuntimeError, match="OmaRag API 302"):
        api.request("GET", "/v1/workspaces")
    assert requests == ["/v1/workspaces"]


async def test_mcp_exposes_read_only_tools_by_default() -> None:
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "omarag_ask",
        "omarag_job_status",
        "omarag_list_workspaces",
        "omarag_quality",
        "omarag_search",
    }
    assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools)
    assert all(tool.annotations and not tool.annotations.destructiveHint for tool in tools)
