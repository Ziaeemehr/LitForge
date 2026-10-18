from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field, model_validator


class EvidenceItem(BaseModel):
    id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    page: int = Field(ge=1)
    section: str | None = None
    kind: str = "text"
    from_image: bool = False


class EvidencePacket(BaseModel):
    items: list[EvidenceItem] = Field(min_length=1)
    sensitive: bool = False
    content_hash: str = ""
    dropped_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> EvidencePacket:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence IDs must be unique")
        payload = {"items": [item.model_dump() for item in self.items], "sensitive": self.sensitive}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        object.__setattr__(self, "content_hash", hashlib.sha256(raw.encode()).hexdigest())
        return self
