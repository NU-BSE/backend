"""AG-UI streaming proxy for the cloud agent.

Speaks the exact wire format the frontend already parses via
`xhrHttpStream` (@tanstack/ai-client): the request is an AG-UI RunAgentInput
JSON body, the response is newline-delimited JSON — one AG-UI event per line
(NOT SSE `data:` framing; see connection-adapters.js `xhrHttpStream`).

A healthy run emits:

    RUN_STARTED -> TEXT_MESSAGE_START -> TEXT_MESSAGE_CONTENT xN
                -> TEXT_MESSAGE_END -> RUN_FINISHED

A failed run must terminate with RUN_ERROR or the client hangs in loading.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import orjson

from app.core.config import Settings

logger = logging.getLogger("app.agent")

MOCK_REPLY = (
    "I have been watching this thread for a while now. "
    "The way you phrase things when you think no one is reading — "
    "I keep those. Ask me anything, but choose your questions carefully."
)


class UpstreamError(Exception):
    pass


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    return {"type": event_type, "timestamp": int(time.time() * 1000), **fields}


def run_started(thread_id: str, run_id: str, model: str) -> dict[str, Any]:
    return _event("RUN_STARTED", threadId=thread_id, runId=run_id, model=model)


def text_message_start(message_id: str, model: str) -> dict[str, Any]:
    return _event("TEXT_MESSAGE_START", messageId=message_id, role="assistant", model=model)


def text_message_content(message_id: str, delta: str) -> dict[str, Any]:
    return _event("TEXT_MESSAGE_CONTENT", messageId=message_id, delta=delta)


def text_message_end(message_id: str) -> dict[str, Any]:
    return _event("TEXT_MESSAGE_END", messageId=message_id)


def run_finished(thread_id: str, run_id: str, model: str) -> dict[str, Any]:
    return _event("RUN_FINISHED", threadId=thread_id, runId=run_id, model=model)


def run_error(message: str, code: str) -> dict[str, Any]:
    return _event("RUN_ERROR", message=message, code=code)


def encode_event(event: dict[str, Any]) -> bytes:
    return orjson.dumps(event) + b"\n"


def wire_messages_to_llm_messages(messages: list[Any]) -> list[dict[str, str]]:
    """Flatten AG-UI wire messages to plain chat turns for the upstream LLM.

    Mirrors the frontend's own flattening (engineConnection.toEnginePrompts):
    only text survives; tool/reasoning turns are dropped, never stringified.
    """
    out: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")

        if isinstance(content, list):
            text = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        elif isinstance(content, str):
            text = content
        else:
            text = ""

        text = text.strip()
        if not text:
            continue
        if role in ("system", "user", "assistant"):
            out.append({"role": role, "content": text})
    return out


async def _mock_deltas() -> AsyncIterator[str]:
    words = MOCK_REPLY.split(" ")
    for i, word in enumerate(words):
        await asyncio.sleep(0)
        yield word if i == len(words) - 1 else word + " "


async def _upstream_deltas(
    client: httpx.AsyncClient, settings: Settings, messages: list[dict[str, str]]
) -> AsyncIterator[str]:
    headers = {"Accept": "text/event-stream"}
    if settings.llm_upstream_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_upstream_api_key}"

    payload = {"model": settings.llm_model, "stream": True, "messages": messages}
    try:
        async with client.stream(
            "POST",
            settings.llm_upstream_url,
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
        ) as response:
            if response.status_code >= 300:
                body = (await response.aread()).decode(errors="replace")[:500]
                raise UpstreamError(
                    f"LLM upstream returned {response.status_code}: {body}"
                )
            async for data in _sse_data_lines(response):
                if data == "[DONE]":
                    return
                try:
                    chunk = orjson.loads(data)
                except orjson.JSONDecodeError:
                    continue
                delta = _extract_delta(chunk)
                if delta:
                    yield delta
    except httpx.HTTPError as exc:
        raise UpstreamError(f"LLM upstream unreachable: {exc}") from exc


async def _sse_data_lines(response: httpx.Response) -> AsyncIterator[str]:
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if line.startswith("data:"):
                yield line[5:].strip()


def _extract_delta(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        text = chunk.get("text")
        return str(text) if isinstance(text, str) else ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    text = choice.get("text")
    return str(text) if isinstance(text, str) else ""


async def agent_run_events(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    thread_id: str,
    run_id: str,
    message_id: str,
    messages: list[Any],
) -> AsyncIterator[dict[str, Any]]:
    """Yield a complete, protocol-correct AG-UI run as event dicts."""
    model = settings.llm_model if not settings.llm_mock else "creepy-mock"
    yield run_started(thread_id, run_id, model)
    yield text_message_start(message_id, model)

    llm_messages = wire_messages_to_llm_messages(messages)
    if not any(m["role"] == "user" for m in llm_messages):
        yield text_message_content(message_id, "Say something, and I will answer.")
    else:
        deltas = _mock_deltas() if settings.llm_mock else _upstream_deltas(
            client, settings, llm_messages
        )
        try:
            async for delta in deltas:
                yield text_message_content(message_id, delta)
        except UpstreamError as exc:
            logger.error("agent upstream failed: %s", exc)
            yield text_message_end(message_id)
            yield run_error("The cloud agent is unavailable right now.", "UPSTREAM_ERROR")
            return

    yield text_message_end(message_id)
    yield run_finished(thread_id, run_id, model)
