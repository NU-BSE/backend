from typing import Any
from unittest.mock import AsyncMock

import httpx
import orjson
import pytest

from app.core.config import Settings
from app.llm.errors import OpenRouterError
from app.llm.openrouter import (
    extract_message,
    extract_usage,
    openrouter_chat_completion,
)


def _settings(**kwargs) -> Settings:
    defaults: dict[str, object] = {
        "jwt_secret": "test-secret-" + "x" * 40,
        "brevo_api_key": "test",
        "email_from": "test@test.com",
        "openrouter_api_key": "sk-test",
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "llm_request_timeout_seconds": 10.0,
    }
    return Settings(_env_file=None, **defaults, **kwargs)  # type: ignore[arg-type]


def _mock_response(status_code: int, json_body: dict[str, Any]) -> AsyncMock:
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = status_code
    mock_resp.aread = AsyncMock(
        return_value=orjson.dumps(json_body)
    )
    return mock_resp


class TestExtractMessage:

    def test_extracts_assistant_message(self):
        completion = {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}]
        }
        msg = extract_message(completion)
        assert msg == {"role": "assistant", "content": "hello"}

    def test_extracts_tool_call_message(self):
        completion = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "t1", "function": {"name": "search", "arguments": "{}"}}],
                }
            }]
        }
        msg = extract_message(completion)
        assert msg["role"] == "assistant"
        assert "tool_calls" in msg

    def test_no_choices_returns_empty(self):
        completion: dict[str, Any] = {"choices": []}
        msg = extract_message(completion)
        assert msg == {"role": "assistant", "content": ""}

    def test_missing_choices_returns_empty(self):
        completion: dict[str, Any] = {}
        msg = extract_message(completion)
        assert msg == {"role": "assistant", "content": ""}

    def test_choices_is_string_returns_empty(self):
        completion: dict[str, Any] = {"choices": "invalid"}
        msg = extract_message(completion)
        assert msg == {"role": "assistant", "content": ""}

    def test_choice_not_dict(self):
        completion: dict[str, Any] = {"choices": [None]}
        msg = extract_message(completion)
        assert msg == {"role": "assistant", "content": ""}


class TestExtractUsage:

    def test_extracts_usage(self):
        completion = {
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        }
        usage = extract_usage(completion)
        assert usage == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    def test_no_usage_returns_empty(self):
        completion: dict[str, Any] = {}
        usage = extract_usage(completion)
        assert usage == {}

    def test_usage_not_dict_returns_empty(self):
        completion: dict[str, Any] = {"usage": "invalid"}
        usage = extract_usage(completion)
        assert usage == {}


class TestOpenRouterChatCompletion:

    async def test_successful_request(self):
        settings = _settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _mock_response(
            200, {"choices": [{"message": {"content": "ok"}}]}
        )

        result = await openrouter_chat_completion(
            mock_client,
            settings,
            model_id="anthropic/claude-3-haiku",
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result["choices"][0]["message"]["content"] == "ok"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["json"]["model"] == "anthropic/claude-3-haiku"

    async def test_request_includes_tools(self):
        settings = _settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _mock_response(
            200, {"choices": [{"message": {"content": "tool result"}}]}
        )
        tools = [{"type": "function", "function": {"name": "search"}}]

        result = await openrouter_chat_completion(
            mock_client,
            settings,
            model_id="test-model",
            messages=[{"role": "user", "content": "search"}],
            tools=tools,
        )

        assert "choices" in result
        call_kwargs = mock_client.post.call_args
        assert "tools" in call_kwargs[1]["json"]
        assert call_kwargs[1]["json"]["tools"] == tools

    async def test_400_error(self):
        settings = _settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _mock_response(
            400, {"error": "bad request"}
        )

        with pytest.raises(OpenRouterError) as exc_info:
            await openrouter_chat_completion(
                mock_client,
                settings,
                model_id="test-model",
                messages=[],
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.retryable is False

    async def test_500_error(self):
        settings = _settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _mock_response(
            500, {"error": "internal"}
        )

        with pytest.raises(OpenRouterError) as exc_info:
            await openrouter_chat_completion(
                mock_client,
                settings,
                model_id="test-model",
                messages=[],
            )
        assert exc_info.value.status_code == 500
        assert exc_info.value.retryable is True

    async def test_retry_on_500(self):
        settings = _settings(**{"llm_max_retries": 1})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = [
            _mock_response(500, {"error": "transient"}),
            _mock_response(200, {"choices": [{"message": {"content": "recovered"}}]}),
        ]

        result = await openrouter_chat_completion(
            mock_client,
            settings,
            model_id="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result["choices"][0]["message"]["content"] == "recovered"
        assert mock_client.post.call_count == 2

    async def test_retry_exhausted_on_500(self):
        settings = _settings(**{"llm_max_retries": 1})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = [
            _mock_response(500, {"error": "fail1"}),
            _mock_response(500, {"error": "fail2"}),
        ]

        with pytest.raises(OpenRouterError) as exc_info:
            await openrouter_chat_completion(
                mock_client,
                settings,
                model_id="test-model",
                messages=[],
            )
        assert exc_info.value.status_code == 500
        assert mock_client.post.call_count == 2

    async def test_http_error_retry(self):
        settings = _settings(**{"llm_max_retries": 1})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = [
            httpx.ConnectError("connection refused"),
            _mock_response(200, {"choices": [{"message": {"content": "ok"}}]}),
        ]

        result = await openrouter_chat_completion(
            mock_client,
            settings,
            model_id="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result["choices"][0]["message"]["content"] == "ok"
        assert mock_client.post.call_count == 2

    async def test_401_not_retried(self):
        settings = _settings(**{"llm_max_retries": 2})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _mock_response(
            401, {"error": "unauthorized"}
        )

        with pytest.raises(OpenRouterError) as exc_info:
            await openrouter_chat_completion(
                mock_client,
                settings,
                model_id="test-model",
                messages=[],
            )
        assert exc_info.value.status_code == 401
        assert exc_info.value.retryable is False
        assert mock_client.post.call_count == 1
