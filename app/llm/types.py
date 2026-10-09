"""Provider-neutral shapes for talking to a language model.

A provider (Bedrock, OpenAI, ...) is one class that implements `LLMClient` by translating these
to and from its own API. Nothing else in the app knows which provider is in use.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema for the tool's arguments


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    output: dict[str, Any]  # JSON-safe


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant messages
    tool_results: list[ToolResult] = field(default_factory=list)  # tool messages


@dataclass
class LLMTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMUnavailable(Exception):
    """The model could not be reached or refused to answer."""


class LLMClient(Protocol):
    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn: ...
