from __future__ import annotations

from pydantic import BaseModel, Field


class BudgetExceeded(ValueError):
    pass


class Pricing(BaseModel):
    input_per_million_usd: float = Field(ge=0, allow_inf_nan=False)
    output_per_million_usd: float = Field(ge=0, allow_inf_nan=False)


class BudgetGuard:
    def __init__(self, max_per_call_usd: float, max_per_run_usd: float,
                 max_per_month_usd: float | None = None) -> None:
        self.max_per_call_usd = max_per_call_usd
        self.max_per_run_usd = max_per_run_usd
        self.max_per_month_usd = max_per_month_usd

    @staticmethod
    def _cost(pricing: Pricing, input_tokens: int, output_tokens: int) -> float:
        return ((input_tokens * pricing.input_per_million_usd)
                + (output_tokens * pricing.output_per_million_usd)) / 1_000_000

    def _enforce(self, cost: float, spent_usd: float, spent_month_usd: float) -> float:
        if cost > self.max_per_call_usd:
            raise BudgetExceeded("cost exceeds per-call limit")
        if spent_usd + cost > self.max_per_run_usd:
            raise BudgetExceeded("cost exceeds run limit")
        if self.max_per_month_usd is not None and spent_month_usd + cost > self.max_per_month_usd:
            raise BudgetExceeded("cost exceeds month limit")
        return cost

    def check(self, pricing: Pricing | None, input_tokens: int, max_output_tokens: int, spent_usd: float,
              spent_month_usd: float = 0) -> float:
        if pricing is None:
            raise BudgetExceeded("pricing is required for remote routing")
        return self._enforce(self._cost(pricing, input_tokens, max_output_tokens), spent_usd, spent_month_usd)

    def check_actual(self, pricing: Pricing, input_tokens: int, output_tokens: int, spent_usd: float,
                     spent_month_usd: float = 0) -> float:
        return self._enforce(self._cost(pricing, input_tokens, output_tokens), spent_usd, spent_month_usd)
