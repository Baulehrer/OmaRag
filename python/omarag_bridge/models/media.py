from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .domain import CitationAnchor, StrictModel

MediaKind = Literal["figure", "diagram", "table", "formula"]
MediaTextOrigin = Literal[
    "native-caption",
    "ocr",
    "nearby-text",
    "model-derived",
    "human-reviewed",
]
MediaLinkOrigin = Literal["deterministic", "source-text", "model-derived", "human-reviewed"]
MediaRelation = Literal[
    "section_contains_media",
    "evidence_depicts_media",
    "evidence_context_for_media",
    "media_mentions_term",
    "media_duplicate_of",
    "media_variant_of",
]


class NormalizedMediaBBox(StrictModel):
    """Top-left-origin page coordinates shared by Core, API clients and crops."""

    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    coordinate_space: Literal["normalized"] = "normalized"

    @model_validator(mode="after")
    def ordered(self) -> NormalizedMediaBBox:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("Media bbox must have positive width and height")
        return self


class MediaText(StrictModel):
    """Text attached to a visual with an explicit, non-interchangeable origin."""

    text: str = Field(min_length=1)
    origin: MediaTextOrigin
    source_ref: str | None = None
    evidence_id: str | None = None
    model_digest: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def origin_contract(self) -> MediaText:
        if self.origin == "nearby-text" and self.evidence_id is None:
            raise ValueError("Nearby media text must identify its evidence record")
        if self.origin == "model-derived" and self.model_digest is None:
            raise ValueError("Model-derived media text must pin the model digest")
        if self.origin != "model-derived" and self.model_digest is not None:
            raise ValueError("Only model-derived media text may carry a model digest")
        return self


class MediaAsset(StrictModel):
    """One page-local visual with stable source and crop provenance.

    A MediaAsset is not itself textual evidence. ``nearby_text`` only links back
    to existing EvidenceRecords, while ``derived_text`` is routing metadata and
    must never be promoted to a factual text citation without human review.
    """

    media_id: str
    logical_document_id: str
    generation_id: str
    source_fingerprint: str
    page_no: int = Field(ge=1)
    page_label: str
    doc_item_ref: str
    bbox: NormalizedMediaBBox
    kind: MediaKind
    section_node_id: str
    crop_version: str
    captions: list[MediaText] = Field(default_factory=list)
    ocr_text: list[MediaText] = Field(default_factory=list)
    nearby_text: list[MediaText] = Field(default_factory=list)
    derived_text: list[MediaText] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    pixel_sha256: str | None = None
    perceptual_hash: str | None = None
    width_px: int | None = Field(default=None, ge=1)
    height_px: int | None = Field(default=None, ge=1)
    mime_type: str | None = None
    crop_resource: str | None = None
    thumbnail_resource: str | None = None
    quality_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_text_origins(self) -> MediaAsset:
        expected = {
            "captions": "native-caption",
            "ocr_text": "ocr",
            "nearby_text": "nearby-text",
        }
        for field_name, origin in expected.items():
            if any(item.origin != origin for item in getattr(self, field_name)):
                raise ValueError(f"{field_name} may only contain {origin} text")
        if any(
            item.origin not in {"model-derived", "human-reviewed"} for item in self.derived_text
        ):
            raise ValueError("derived_text may only contain model or human reviewed text")
        linked = set(self.evidence_ids)
        if any(item.evidence_id not in linked for item in self.nearby_text):
            raise ValueError("Nearby text must reference a linked evidence record")
        resources = (self.crop_resource, self.thumbnail_resource)
        if any(resources) and not all(resources):
            raise ValueError("Crop and thumbnail resources must be present together")
        return self

    def routing_text(self, *, include_model_derived: bool = True) -> str:
        """Return bounded index text without turning it into answer evidence."""

        parts = [item.text for item in [*self.captions, *self.ocr_text, *self.nearby_text]]
        if include_model_derived:
            parts.extend(item.text for item in self.derived_text)
        return "\n".join(dict.fromkeys(part.strip() for part in parts if part.strip()))


class MediaLink(StrictModel):
    link_id: str
    source_id: str
    target_id: str
    relation: MediaRelation
    origin: MediaLinkOrigin
    weight: float = Field(default=1.0, ge=0.0)
    evidence_ids: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    model_digest: str | None = None

    @model_validator(mode="after")
    def derived_links_pin_model(self) -> MediaLink:
        if self.origin == "model-derived" and self.model_digest is None:
            raise ValueError("Model-derived media links must pin the model digest")
        if self.origin != "model-derived" and self.model_digest is not None:
            raise ValueError("Only model-derived media links may carry a model digest")
        return self


class MediaDuplicateGroup(StrictModel):
    canonical_media_id: str
    member_media_ids: list[str] = Field(min_length=2)
    match: Literal["exact", "perceptual"]


class BookMediaSnapshot(StrictModel):
    assets: list[MediaAsset] = Field(default_factory=list)
    links: list[MediaLink] = Field(default_factory=list)
    duplicate_groups: list[MediaDuplicateGroup] = Field(default_factory=list)
    dense_index_generation: str | None = None
    dense_model_digest: str | None = None


class MediaEvidence(StrictModel):
    """Rust/App-compatible query result; never a textual EvidenceRecord."""

    media_id: str
    kind: str
    document_id: str | None = None
    document_title: str | None = None
    page: int | None = Field(default=None, ge=1)
    bbox: NormalizedMediaBBox | None = None
    caption: str | None = None
    caption_origin: str | None = None
    score: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    thumbnail_url: str | None = None
    preview_url: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class PageEvidence(StrictModel):
    page_id: str
    citation_index: int | None = Field(default=None, ge=0)
    document_id: str | None = None
    document_title: str | None = None
    page: int = Field(ge=1)
    score: float | None = None
    primary_anchors: list[CitationAnchor] = Field(default_factory=list)
    context_anchors: list[CitationAnchor] = Field(default_factory=list)
    preview_url: str | None = None


class VisualEvidenceSelection(StrictModel):
    max_media: int = Field(default=4, ge=0, le=4)
    cut_reason: str | None = None


class VisualEvidenceResponse(StrictModel):
    """Stable App/TUI payload: cited pages first, then zero to four real crops."""

    schema_version: Literal[1] = 1
    pages: list[PageEvidence] = Field(default_factory=list)
    media: list[MediaEvidence] = Field(default_factory=list, max_length=4)
    selection: VisualEvidenceSelection = Field(default_factory=VisualEvidenceSelection)


# Source-compatible names for earlier Core callers; serialized fields follow
# the shared Rust contract above.
VisualEvidence = MediaEvidence
PagePreviewEvidence = PageEvidence


class OKFMediaSource(StrictModel):
    resource: str
    title: str | None = None
    page: int = Field(ge=1)
    source_ref: str


class OKFGenerated(StrictModel):
    actor: str
    model_digest: str


class OKFMediaProposal(StrictModel):
    """OKF 0.2-compatible proposal; persistence belongs to OmaWiki."""

    type: Literal["Book Figure", "Book Diagram", "Book Table", "Book Formula"]
    title: str
    description: str | None = None
    resource: str
    sources: list[OKFMediaSource] = Field(min_length=1)
    status: Literal["draft"] = "draft"
    generated: OKFGenerated | None = None
    omarag: dict[str, Any]
