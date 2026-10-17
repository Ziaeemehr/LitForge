"""Safe, typed analysis errors for persisted run provenance."""

from pydantic import ValidationError

from litforge.analysis.gateway import GatewayError
from litforge.llm.base import LlmError
from litforge.llm.budget import BudgetExceeded
from litforge.llm.router import RoutingError


def run_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        message = "invalid model response"
    elif isinstance(exc, GatewayError | LlmError | BudgetExceeded | RoutingError):
        # Gateway sanitises provider exceptions before exposing these errors.
        message = str(exc)
    else:
        message = "analysis failed"
    return f"{type(exc).__name__}: {message}"
