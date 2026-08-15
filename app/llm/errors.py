from __future__ import annotations


class LLMError(Exception):
    def __init__(self, message: str, *, code: str = "LLM_ERROR", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RoutingValidationError(LLMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="ROUTING_VALIDATION_ERROR", retryable=False)


class ExpertBudgetExhausted(LLMError):
    def __init__(self, reason: str = "expert_budget_unavailable") -> None:
        super().__init__("Expert budget exhausted", code=reason, retryable=False)


class OpenRouterError(LLMError):
    def __init__(self, status_code: int, body: str) -> None:
        retryable = (
            status_code == 0
            or status_code == 408
            or status_code == 429
            or status_code >= 500
        )
        super().__init__(
            f"OpenRouter returned {status_code}: {body}",
            code="OPENROUTER_ERROR",
            retryable=retryable,
        )
        self.status_code = status_code
