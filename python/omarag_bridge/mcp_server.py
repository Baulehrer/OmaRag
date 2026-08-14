from __future__ import annotations

import os
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


class OmaRagApi:
    """Small HTTP boundary used by the optional, separate MCP process."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(
            base_url=(base_url or os.getenv("OMARAG_URL", "http://127.0.0.1:8765")).rstrip("/"),
            headers=headers,
            timeout=30,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except ValueError:
                detail = response.text
            raise RuntimeError(f"OmaRag API {response.status_code}: {detail}") from exc
        return response.json()

    def ask(self, workspace_id: str, question: str, evidence_mode: str) -> dict[str, Any]:
        run = self.request(
            "POST",
            f"/v1/workspaces/{workspace_id}/runs",
            json={"question": question, "evidence_mode": evidence_mode},
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = self.request("GET", f"/v1/runs/{run['id']}")
            if current["status"] in {"completed", "failed", "cancelled"}:
                return current
            time.sleep(0.2)
        raise RuntimeError("OmaRag-Antwort hat das Zeitlimit von 120 Sekunden ueberschritten")


api = OmaRagApi(token=os.getenv("OMARAG_TOKEN"))
mcp = FastMCP(
    "OmaRag",
    instructions="Read-only Zugriff auf explizite OmaRag-Workspaces.",
)
readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


@mcp.tool(annotations=readonly)
def omarag_list_workspaces() -> list[dict[str, Any]]:
    """Listet erreichbare OmaRag-Workspaces ohne Aenderungen."""
    return api.request("GET", "/v1/workspaces")


@mcp.tool(annotations=readonly)
def omarag_search(workspace_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Durchsucht einen expliziten Workspace und liefert stabile Quellenreferenzen."""
    if not 1 <= limit <= 50:
        raise ValueError("limit muss zwischen 1 und 50 liegen")
    return api.request(
        "POST",
        f"/v1/workspaces/{workspace_id}/search",
        json={"query": query, "limit": limit},
    )


@mcp.tool(annotations=readonly)
def omarag_ask(workspace_id: str, question: str, evidence_mode: str = "strict") -> dict[str, Any]:
    """Stellt eine Frage; der Wissensbestand selbst bleibt unveraendert."""
    if evidence_mode not in {"strict", "normal", "explore"}:
        raise ValueError("evidence_mode muss strict, normal oder explore sein")
    return api.ask(workspace_id, question, evidence_mode)


@mcp.tool(annotations=readonly)
def omarag_job_status(job_id: str) -> dict[str, Any]:
    """Liest Status, Fortschritt und Fehler eines Hintergrundauftrags."""
    return api.request("GET", f"/v1/jobs/{job_id}")


@mcp.tool(annotations=readonly)
def omarag_quality(workspace_id: str) -> dict[str, Any]:
    """Liest den aktuellen Qualitaetsbericht eines Workspaces."""
    return api.request("GET", f"/v1/workspaces/{workspace_id}/quality")


@mcp.resource("omarag://workspaces/{workspace_id}/documents")
def workspace_documents(workspace_id: str) -> list[dict[str, Any]]:
    """Indexierte Dokumente eines expliziten Workspaces."""
    return api.request("GET", f"/v1/workspaces/{workspace_id}/documents")


@mcp.resource("omarag://workspaces/{workspace_id}/sources")
def workspace_sources(workspace_id: str) -> list[dict[str, Any]]:
    """Konfigurierte Quellen eines expliziten Workspaces."""
    return api.request("GET", f"/v1/workspaces/{workspace_id}/sources")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
