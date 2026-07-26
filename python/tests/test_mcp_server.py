from __future__ import annotations

import httpx

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
