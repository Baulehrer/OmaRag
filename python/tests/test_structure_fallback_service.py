from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from omarag_bridge.models.book import BookLine, BookPage, NavigationRegion
from omarag_bridge.services.structure_fallback_service import (
    OllamaStructureFallbackRunner,
    StructureFallbackRequest,
    is_local_structure_endpoint,
    refine_uncertain_navigation_regions,
)


@pytest.mark.asyncio
async def test_default_ollama_runner_uses_installed_model_and_structured_chat() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen:1b", "digest": "abc"}]},
            )
        body = json.loads(request.content)
        assert body["stream"] is False
        assert body["format"]["type"] == "object"
        assert body["options"] == {"temperature": 0, "seed": 0, "num_predict": 128}
        assert body["think"] is False
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": '{"selections":[]}'}},
        )

    runner = OllamaStructureFallbackRunner(transport=httpx.MockTransport(handle))
    result = await runner.run(
        StructureFallbackRequest(
            endpoint="http://127.0.0.1:11434",
            model="qwen:1b",
            system="Return routing JSON.",
            payload={"candidates": []},
            json_schema={"type": "object"},
            max_output_tokens=128,
            expected_digest="abc",
        )
    )

    assert json.loads(result) == {"selections": []}
    assert [request.url.path for request in seen] == [
        "/api/tags",
        "/api/chat",
        "/api/tags",
    ]


def _page(page_no: int, *, prefix: str = "Chapter") -> BookPage:
    return BookPage(
        page_no=page_no,
        page_label=str(page_no),
        lines=[
            BookLine(
                page_no=page_no,
                text=f"{prefix} Alpha ........ 12",
                source_ref=f"#/texts/{page_no}-1",
            ),
            BookLine(
                page_no=page_no,
                text=f"{prefix} Beta ........ 18",
                source_ref=f"#/texts/{page_no}-2",
            ),
            BookLine(
                page_no=page_no,
                text=f"{prefix} Gamma ........ 24",
                source_ref=f"#/texts/{page_no}-3",
            ),
        ],
    )


def _region(page_no: int, *, score: float = 0.65) -> NavigationRegion:
    return NavigationRegion(
        role="toc",
        page_start=page_no,
        page_end=page_no,
        score=score,
        accepted=False,
        metrics={"entry_count": 3.0},
    )


class SelectingRunner:
    def __init__(self, *, selection_count: int = 3) -> None:
        self.selection_count = selection_count
        self.requests: list[StructureFallbackRequest] = []

    async def run(self, request: StructureFallbackRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        candidates = request.payload["candidates"][: self.selection_count]
        selections: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            parent_id = candidates[0]["candidate_id"] if index == 1 else None
            selections.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "substring": candidate["allowed_substrings"][-1],
                    "locator": candidate["allowed_locators"][0],
                    "role": "toc",
                    "level": 1 if index == 1 else 0,
                    "parent_id": parent_id,
                }
            )
        return {"selections": selections}


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:11434",
        "https://[::1]:11434/",
        "http://localhost:11434",
        "unix:///run/omarag/structure.sock",
    ],
)
def test_structure_endpoint_accepts_only_explicit_local_transports(endpoint: str) -> None:
    assert is_local_structure_endpoint(endpoint) is True


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "http://192.168.1.4:11434",
        "https://models.example.com",
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:11434/api/chat",
        "unix://remote/run/model.sock",
        "unix:///run/../tmp/model.sock",
    ],
)
def test_structure_endpoint_rejects_remote_or_ambiguous_targets(
    endpoint: str | None,
) -> None:
    assert is_local_structure_endpoint(endpoint) is False


@pytest.mark.asyncio
async def test_local_fallback_is_bounded_schema_driven_and_route_only() -> None:
    pages = [_page(2)]
    region = _region(2)
    runner = SelectingRunner()

    result = await refine_uncertain_navigation_regions(
        pages=pages,
        regions=[region],
        total_pages=120,
        endpoint="http://127.0.0.1:11434",
        model="qwen3.5:4b",
        runner=runner,
    )

    assert result.used is True
    assert result.calls == 1
    assert result.applied_regions == 1
    assert result.regions[0].accepted is True
    assert result.regions[0].score >= region.score + 0.05
    assert [selection.page_no for selection in result.selections] == [2, 2, 2]
    assert [selection.locator for selection in result.selections] == ["12", "18", "24"]
    request = runner.requests[0]
    assert request.temperature == 0
    assert request.seed == 0
    assert request.allow_download is False
    assert request.max_output_tokens <= 1024
    assert request.json_schema["additionalProperties"] is False
    item_schema = request.json_schema["properties"]["selections"]["items"]
    assert item_schema["additionalProperties"] is False
    assert "page_no" not in item_schema["properties"]
    assert pages == [_page(2)]


@pytest.mark.asyncio
async def test_fallback_never_calls_more_than_four_regions_per_book() -> None:
    pages = [_page(page_no) for page_no in range(1, 7)]
    regions = [_region(page_no) for page_no in range(1, 7)]
    runner = SelectingRunner()

    result = await refine_uncertain_navigation_regions(
        pages=pages,
        regions=regions,
        total_pages=100,
        endpoint="unix:///run/omarag/structure.sock",
        model="local-structure:latest",
        runner=runner,
    )

    assert len(runner.requests) == result.calls == 4
    assert result.applied_regions == 4
    assert result.candidate_regions == 6
    assert result.skipped_regions == 2


@pytest.mark.asyncio
async def test_deterministic_confidence_at_or_above_point_82_is_immutable() -> None:
    protected = _region(1, score=0.82)
    low = _region(2, score=0.65)
    runner = SelectingRunner()

    result = await refine_uncertain_navigation_regions(
        pages=[_page(1), _page(2)],
        regions=[protected, low],
        total_pages=80,
        endpoint="http://[::1]:11434",
        model="local-structure",
        runner=runner,
    )

    assert result.regions[0] == protected
    assert result.regions[1].accepted is True
    assert result.calls == 1


@pytest.mark.asyncio
async def test_glossary_substrings_are_selected_from_existing_definition_lines() -> None:
    page = BookPage(
        page_no=90,
        page_label="90",
        lines=[
            BookLine(page_no=90, text="Beton: künstlicher Stein", source_ref="#/g1"),
            BookLine(page_no=90, text="Mörtel: Bindemittelgemisch", source_ref="#/g2"),
        ],
    )
    region = NavigationRegion(
        role="glossary",
        page_start=90,
        page_end=90,
        score=0.65,
        accepted=False,
    )

    class GlossaryRunner:
        async def run(self, request: StructureFallbackRequest) -> Mapping[str, Any]:
            return {
                "selections": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "substring": candidate["allowed_substrings"][-1],
                        "locator": None,
                        "role": "glossary",
                        "level": 0,
                        "parent_id": None,
                    }
                    for candidate in request.payload["candidates"]
                ]
            }

    result = await refine_uncertain_navigation_regions(
        pages=[page],
        regions=[region],
        total_pages=100,
        endpoint="http://127.0.0.1:11434",
        model="local-structure",
        runner=GlossaryRunner(),
    )

    assert result.used is True
    assert [selection.substring for selection in result.selections] == ["Beton", "Mörtel"]
    assert all(selection.page_no == 90 for selection in result.selections)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["page", "substring", "locator", "depth", "cycle"])
async def test_fallback_rejects_mutation_and_invalid_hierarchy(mutation: str) -> None:
    class MutatingRunner:
        async def run(self, request: StructureFallbackRequest) -> Mapping[str, Any]:
            first, second = request.payload["candidates"][:2]
            one = {
                "candidate_id": first["candidate_id"],
                "substring": first["allowed_substrings"][0],
                "locator": first["allowed_locators"][0],
                "role": "toc",
                "level": 0,
                "parent_id": None,
            }
            two = {
                "candidate_id": second["candidate_id"],
                "substring": second["allowed_substrings"][0],
                "locator": second["allowed_locators"][0],
                "role": "toc",
                "level": 1,
                "parent_id": first["candidate_id"],
            }
            if mutation == "page":
                one["page_no"] = 999
            elif mutation == "substring":
                one["substring"] = "invented title"
            elif mutation == "locator":
                one["locator"] = "999"
            elif mutation == "depth":
                one["level"] = 7
            else:
                one["level"] = 2
                one["parent_id"] = second["candidate_id"]
                two["parent_id"] = first["candidate_id"]
            return {"selections": [one, two]}

    region = _region(1)
    result = await refine_uncertain_navigation_regions(
        pages=[_page(1)],
        regions=[region],
        total_pages=80,
        endpoint="http://localhost:11434",
        model="local-structure",
        runner=MutatingRunner(),
    )

    assert result.regions == [region]
    assert result.used is False
    assert result.failures


@pytest.mark.asyncio
async def test_fallback_requires_objective_gain_and_never_blocks_on_runner_failure() -> None:
    weak = SelectingRunner(selection_count=1)
    region = _region(1, score=0.74)
    weak_result = await refine_uncertain_navigation_regions(
        pages=[_page(1)],
        regions=[region],
        total_pages=100,
        endpoint="http://127.0.0.1:11434",
        model="local-structure",
        runner=weak,
    )

    class BrokenRunner:
        async def run(self, _request: StructureFallbackRequest) -> Mapping[str, Any]:
            raise RuntimeError("local runtime down")

    broken_result = await refine_uncertain_navigation_regions(
        pages=[_page(1)],
        regions=[region],
        total_pages=100,
        endpoint="http://127.0.0.1:11434",
        model="local-structure",
        runner=BrokenRunner(),
    )

    assert weak_result.regions == [region]
    assert weak_result.failures == ("objective-not-improved",)
    assert broken_result.regions == [region]
    assert broken_result.failures == ("runner-runtimeerror",)


@pytest.mark.asyncio
async def test_default_without_injected_runner_is_fail_safe_and_unused() -> None:
    region = _region(1)

    result = await refine_uncertain_navigation_regions(
        pages=[_page(1)],
        regions=[region],
        total_pages=100,
        endpoint="http://127.0.0.1:11434",
        model="local-structure",
        runner=None,
    )

    assert result.regions == [region]
    assert result.calls == 0
    assert result.used is False
    assert result.failures == ("runner-not-configured",)
