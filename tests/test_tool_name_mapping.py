"""Tool-name mapping tests: canonical MCP names -> provider-safe aliases.

OpenAI function names allow only ``[a-zA-Z0-9_-]`` (max 64 chars), but our
MCP tool names use dotted namespaces (``telegram.user.search_chats``). The
backend must map between the two at the OpenRouter boundary, without renaming
the canonical names the frontend's local MCP executor understands.
"""

from __future__ import annotations

import re

import pytest

from app.llm.errors import LLMError
from app.llm.gateway import (
    build_tool_name_maps,
    openrouter_message_to_result,
    provider_safe_name,
    to_openrouter_messages,
    to_openrouter_tools,
)

SAFE_NAME_RE = re.compile(r"[a-zA-Z0-9_-]+")


def assert_provider_safe(name: str) -> None:
    assert name, "provider name must not be empty"
    assert len(name) <= 64, f"provider name too long: {len(name)} {name!r}"
    assert SAFE_NAME_RE.fullmatch(name), f"provider name not safe: {name!r}"


def _tool(name: str) -> dict:
    return {
        "name": name,
        "description": "test tool",
        "inputSchema": {"type": "object", "properties": {}},
    }


def test_telegram_name_is_provider_safe():
    alias = provider_safe_name("telegram.user.search_chats")
    assert_provider_safe(alias)


def test_google_name_is_provider_safe():
    alias = provider_safe_name("google.gmail.search_messages")
    assert_provider_safe(alias)


def test_dotted_names_do_not_collide_with_underscored():
    # a.b and a_b must never map to the same provider alias.
    alias_dotted = provider_safe_name("a.b")
    alias_underscored = provider_safe_name("a_b")
    assert alias_dotted != alias_underscored
    assert_provider_safe(alias_dotted)
    assert_provider_safe(alias_underscored)


def test_long_name_is_truncated_to_64():
    long_name = "telegram.user." + "x" * 100
    alias = provider_safe_name(long_name)
    assert len(alias) <= 64
    assert_provider_safe(alias)


def test_to_openrouter_tools_never_emits_dots():
    tools = [
        _tool("telegram.user.search_chats"),
        _tool("google.gmail.search_messages"),
        _tool("search_chats"),
    ]
    canonical_to_provider, _ = build_tool_name_maps(tools)
    converted = to_openrouter_tools(tools, canonical_to_provider)
    assert len(converted) == 3
    for item in converted:
        assert item["type"] == "function"
        assert_provider_safe(item["function"]["name"])


def test_assistant_tool_calls_mapped_to_provider_name():
    tools = [_tool("telegram.user.search_chats")]
    canonical_to_provider, _ = build_tool_name_maps(tools)

    messages = [
        {"role": "user", "content": "find Daniyar"},
        {
            "role": "assistant",
            "content": "",
            "toolCalls": [
                {
                    "id": "call_1",
                    "toolName": "telegram.user.search_chats",
                    "args": {"query": "Daniyar"},
                }
            ],
        },
    ]

    converted = to_openrouter_messages(messages, canonical_to_provider)
    assistant = converted[1]
    assert assistant["tool_calls"][0]["function"]["name"] == provider_safe_name(
        "telegram.user.search_chats"
    )
    assert "." not in assistant["tool_calls"][0]["function"]["name"]


def test_provider_response_restored_to_canonical():
    tools = [_tool("telegram.user.search_chats")]
    canonical_to_provider, provider_to_canonical = build_tool_name_maps(tools)
    alias = canonical_to_provider["telegram.user.search_chats"]

    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": alias, "arguments": '{"query": "Daniyar"}'},
            }
        ],
    }

    result = openrouter_message_to_result(message, provider_to_canonical)
    assert result.kind == "tool_calls"
    assert result.tool_calls[0].tool_name == "telegram.user.search_chats"
    assert result.tool_calls[0].args == {"query": "Daniyar"}


def test_full_roundtrip_canonical_to_provider_to_canonical():
    canonical = "telegram.user.search_chats"
    tools = [_tool(canonical)]
    canonical_to_provider, provider_to_canonical = build_tool_name_maps(tools)

    # Outbound: definition + assistant history use the provider alias.
    out_tools = to_openrouter_tools(tools, canonical_to_provider)
    alias = out_tools[0]["function"]["name"]
    assert alias != canonical
    assert_provider_safe(alias)

    # Inbound: provider returns the alias, restored to canonical.
    message = {
        "tool_calls": [
            {
                "id": "call_1",
                "function": {"name": alias, "arguments": "{}"},
            }
        ]
    }
    result = openrouter_message_to_result(message, provider_to_canonical)
    assert result.tool_calls[0].tool_name == canonical


def test_simple_compliant_tool_name_unchanged():
    # Names already provider-safe and short keep their identity.
    assert provider_safe_name("search_chats") == "search_chats"
    assert provider_safe_name("send_message") == "send_message"


def test_unknown_provider_name_fails_closed():
    _, provider_to_canonical = build_tool_name_maps(
        [_tool("telegram.user.search_chats")]
    )
    message = {
        "tool_calls": [
            {
                "id": "call_1",
                "function": {"name": "made_up_hallucinated_tool", "arguments": "{}"},
            }
        ]
    }
    with pytest.raises(LLMError) as exc_info:
        openrouter_message_to_result(message, provider_to_canonical)
    assert exc_info.value.code == "TOOL_VALIDATION_ERROR"
    assert exc_info.value.retryable is False
