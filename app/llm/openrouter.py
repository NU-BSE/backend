from __future__ import annotations

import logging
from typing import Any

import httpx
import orjson

from app.core.config import Settings
from app.llm.errors import OpenRouterError

logger = logging.getLogger("app.llm.openrouter")


async def openrouter_chat_completion(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    model_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    url = f"{settings.openrouter_base_url}/chat/completions"

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.openrouter_api_key}",
    }

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }

    if tools:
        payload["tools"] = tools

    timeout = httpx.Timeout(settings.llm_request_timeout_seconds, connect=10.0)

    response: httpx.Response | None = None
    last_exc: Exception | None = None

    for attempt in range(settings.llm_max_retries + 1):
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < settings.llm_max_retries:
                logger.warning("OpenRouter request failed (attempt %d): %s", attempt + 1, exc)
                continue
            raise OpenRouterError(0, str(exc)) from exc

        if response.status_code >= 500:
            body = (await response.aread()).decode(errors="replace")[:500]
            logger.warning(
                "OpenRouter server error %d (attempt %d): %s",
                response.status_code,
                attempt + 1,
                body,
            )
            if attempt < settings.llm_max_retries:
                continue
            raise OpenRouterError(response.status_code, body)

        if response.status_code in (429, 408):
            body = (await response.aread()).decode(errors="replace")[:500]
            logger.warning(
                "OpenRouter rate/timeout %d (attempt %d): %s",
                response.status_code,
                attempt + 1,
                body,
            )
            if attempt < settings.llm_max_retries:
                continue
            raise OpenRouterError(response.status_code, body)

        if response.status_code >= 400:
            body = (await response.aread()).decode(errors="replace")[:500]
            raise OpenRouterError(response.status_code, body)

        data: dict[str, Any] = orjson.loads(await response.aread())
        return data

    assert last_exc is not None
    raise OpenRouterError(0, str(last_exc)) from last_exc


def extract_message(completion: dict[str, Any]) -> dict[str, Any]:
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices:
        return {"role": "assistant", "content": ""}
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(message, dict):
        return message
    return {"role": "assistant", "content": ""}


def extract_usage(completion: dict[str, Any]) -> dict[str, Any]:
    usage = completion.get("usage")
    if isinstance(usage, dict):
        return usage
    return {}
