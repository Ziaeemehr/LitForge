"""Daily, public OpenRouter prices. Missing or stale prices never authorize a call."""

from __future__ import annotations

import json
import math
import time
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from litforge.llm.budget import Pricing
from litforge.llm.files import atomic_write, file_lock


class PriceInfo(BaseModel):
    pricing: Pricing | None = None
    source: str = "openrouter"
    age_s: float | None = None


def _fetch() -> Any:
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=10) as response:  # noqa: S310
        return json.load(response)


FREE_LABEL = "free — often rate-limited; may be unavailable with data_collection: deny"


class OpenRouterPricing:
    _lock: ClassVar[Lock] = Lock()
    _memory: ClassVar[dict[Path, dict[str, Any]]] = {}

    def __init__(self, cache_dir: Path, fetch: Callable[[], Any] = _fetch,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = cache_dir.resolve() / "openrouter-pricing.json"
        self.fetch, self.clock = fetch, clock
        self.read_only = False

    def catalogue(self, *, vision: bool = False, max_input_price: float | None = None,
                  q: str = "") -> list[dict[str, Any]]:
        if max_input_price is not None and (not math.isfinite(max_input_price) or max_input_price < 0):
            raise ValueError("maximum input price must be finite and nonnegative")
        try:
            with self._lock, file_lock(self.path.with_suffix(".lock")):
                self._lookup("")  # same daily cache, failure policy and cross-process lock as prices
                entries = self._memory[self.path]["catalogue"]["data"]
        except (OSError, TimeoutError):
            return []
        result = []
        for entry in entries:
            try:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                    continue
                price = Pricing(input_per_million_usd=float(Decimal(str(entry["pricing"]["prompt"])) * 1_000_000),
                                output_per_million_usd=float(Decimal(str(entry["pricing"]["completion"])) * 1_000_000))
                context = int(entry["context_length"])
                outputs = entry.get("architecture", {}).get("output_modalities", [])
                # Text answers required; audio generators (e.g. music models that also emit text) are not text models.
                if not isinstance(outputs, list) or "text" not in outputs or "audio" in outputs:
                    continue
                modalities = entry.get("architecture", {}).get("input_modalities", ["text"])
                if context <= 0 or not isinstance(modalities, list) or not all(isinstance(m, str) for m in modalities):
                    continue
                name = entry.get("name") if isinstance(entry.get("name"), str) else entry["id"]
                if (vision and "image" not in modalities or
                        max_input_price is not None and price.input_per_million_usd > max_input_price or
                        q.casefold() not in (entry["id"] + " " + name).casefold()):
                    continue
                result.append({"id": entry["id"], "name": name, "context_length": context,
                               "input_modalities": modalities, "output_modalities": outputs,
                               "label": FREE_LABEL if entry["id"].endswith(":free") or not (
                                   price.input_per_million_usd or price.output_per_million_usd) else "",
                               **price.model_dump()})
            except (ArithmeticError, ValueError, TypeError, KeyError, AttributeError):
                continue
        return sorted(result, key=lambda m: (bool(m["label"]), m["input_per_million_usd"],
                                             m["output_per_million_usd"], m["id"]))

    def lookup(self, model: str) -> PriceInfo:
        try:
            if self.read_only:
                with self._lock:
                    return self._lookup(model)
            with self._lock, file_lock(self.path.with_suffix(".lock")):
                return self._lookup(model)
        except (OSError, TimeoutError):
            return PriceInfo()

    def _lookup(self, model: str) -> PriceInfo:
        current = self.clock()
        cached = self._memory.get(self.path, {})
        try:
            # Disk is authoritative under the file lock: another process may have refreshed it.
            if self.path.exists():
                cached = json.loads(self.path.read_text())
            age = current - float(cached["fetched_at"])
            if (not math.isfinite(age) or not 0 <= age < 86400 or
                    not isinstance(cached["catalogue"]["data"], list)):
                cached = {}
        except (OSError, ValueError, KeyError, TypeError):
            cached = {}
        if not cached:
            if self.read_only:
                return PriceInfo()
            try:
                data = self.fetch()
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise ValueError("invalid catalogue")
            except Exception:
                data = {"data": []}  # cache failures too; never repeatedly fetch in the same day
            cached = {"fetched_at": current, "catalogue": data}
            with suppress(OSError):
                atomic_write(self.path, json.dumps(cached))
        self._memory[self.path] = cached
        age = max(0, current - float(cached["fetched_at"]))
        for entry in cached.get("catalogue", {}).get("data", []):
            if isinstance(entry, dict) and entry.get("id") == model:
                try:
                    price = entry["pricing"]
                    pricing = Pricing(input_per_million_usd=float(Decimal(str(price["prompt"])) * 1_000_000),
                                      output_per_million_usd=float(Decimal(str(price["completion"])) * 1_000_000))
                    return PriceInfo(pricing=pricing, age_s=age)
                except (ArithmeticError, ValueError, KeyError, TypeError, ValidationError):
                    break
        return PriceInfo(age_s=age)
