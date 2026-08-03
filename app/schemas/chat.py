from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel


class ChatHttpRequest(CamelModel):
    """AG-UI RunAgentInput as sent by @tanstack/ai-client.

    Parsed leniently: the gate and the proxy only need the ids and messages;
    everything else is accepted and ignored so protocol additions upstream do
    not break the endpoint.
    """

    thread_id: str | None = None
    run_id: str | None = None
    messages: list[Any] = Field(default_factory=list)
    tools: list[Any] = Field(default_factory=list)
    context: list[Any] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    forwarded_props: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
