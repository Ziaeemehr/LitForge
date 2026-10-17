from __future__ import annotations

from pydantic import BaseModel, Field


class BudgetExceeded(ValueError):
    pass


class Pricing(BaseModel):
    input_per_million_usd: float = Field(ge=0)
    output_per_million_usd: float = Field(ge=0)


class BudgetGuard:
    def __init__(self, max_per_call_usd: float, max_per_run_usd: float) -> None:
        self.max_per_call_usd = max_per_call_usd
        self.max_per_run_usd = max_per_run_usd

    def check(self, pricing: Pricing | None, input_tokens: int, max_output_tokens: int, spent_usd: float) -> float:
        if pricing is None:
            raise BudgetExceeded("pricing is required for remote routing")
        estimate = ((input_tokens * pricing.input_per_million_usd)
                    + (max_output_tokens * pricing.output_per_million_usd)) / 1_000_000
        if estimate > self.max_per_call_usd:
            raise BudgetExceeded("estimated cost exceeds per-call limit")
        if spent_usd + estimate > self.max_per_run_usd:
            raise BudgetExceeded("estimated cost exceeds run limit")
        return estimate
