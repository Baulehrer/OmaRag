from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .query_v2 import FusedCandidate, RetrievalCandidate

DEFAULT_RERANKER = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
DEFAULT_RERANKER_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"


def _model_digest(model: str, revision: str | None) -> str:
    material = json.dumps(
        {"model": model, "revision": revision or "default"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass
class PersistentCrossEncoder:
    """One lazy CPU CrossEncoder reused by every request in this process."""

    model_name: str = DEFAULT_RERANKER
    revision: str | None = DEFAULT_RERANKER_REVISION
    cache_folder: Path | None = None
    max_length: int = 512
    batch_size: int = 16
    threads: int = 0
    _model: Any = field(default=None, init=False, repr=False)
    _load_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _predict_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def digest(self) -> str:
        return _model_digest(self.model_name, self.revision)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    async def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)
        return self._model

    def _load(self) -> Any:
        if self.threads > 0:
            os.environ.setdefault("OMP_NUM_THREADS", str(self.threads))
            os.environ.setdefault("MKL_NUM_THREADS", str(self.threads))
        from sentence_transformers import CrossEncoder

        return CrossEncoder(
            self.model_name,
            device="cpu",
            revision=self.revision,
            cache_folder=str(self.cache_folder) if self.cache_folder else None,
            max_length=self.max_length,
        )

    async def score(
        self, question: str, candidates: list[FusedCandidate]
    ) -> list[tuple[RetrievalCandidate, float]]:
        if not candidates:
            return []
        model = await self._ensure_loaded()
        pairs = [[question, self._contextual_text(item.candidate)] for item in candidates]
        async with self._predict_lock:
            scores = await asyncio.to_thread(
                model.predict,
                pairs,
                batch_size=self.batch_size,
                show_progress_bar=False,
                activation_fn=lambda value: value,
                convert_to_numpy=True,
            )
        return [
            (item.candidate, float(score)) for item, score in zip(candidates, scores, strict=True)
        ]

    @staticmethod
    def _contextual_text(candidate: RetrievalCandidate) -> str:
        prefix = " › ".join(candidate.headings)
        return f"{prefix}\n{candidate.content}" if prefix else candidate.content
