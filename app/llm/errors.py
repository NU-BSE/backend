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
        super().__init__(
            f"OpenRouter returned {status_code}: {body}",
            code="OPENROUTER_ERROR",
            retryable=status_code >= 500,
        )
        self.status_code = status_code
