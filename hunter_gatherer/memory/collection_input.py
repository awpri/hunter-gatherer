"""Shared HTTP/MCP validation for reference-only library collections."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class CollectionReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    citation: str = Field(min_length=1, max_length=240)
    status: Literal["unreviewed", "reviewed", "needs_context"] = "unreviewed"
    note: str = Field(default="", max_length=500)


class CollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_sensitive: StrictBool = False
    name: str = Field(min_length=1, max_length=256)
    purpose: str = Field(default="", max_length=2000)
    requirements: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(default_factory=list, max_length=20)
    references: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    voice_examples: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    unresolved: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=2000)
    reviews: list[CollectionReviewInput] = Field(default_factory=list, max_length=70)
    expected_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def references_only(self):
        from hunter_gatherer.memory.collection_schema import (
            collection_reference_citation,
            validate_collection_reference,
        )
        for ref in self.references + self.voice_examples:
            if not validate_collection_reference(ref):
                raise ValueError("References require memory_id, document_id plus chunk_id, or an exact original-file/page identity; source bodies are not stored.")
        citations = {collection_reference_citation(ref) for ref in self.references + self.voice_examples}
        if any(review.citation not in citations for review in self.reviews):
            raise ValueError("Review citations must exactly match a selected reference or voice example.")
        if len({review.citation for review in self.reviews}) != len(self.reviews):
            raise ValueError("Each selected citation may have at most one review state.")
        return self
