from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.llm.bedrock import BedrockLLM
from app.llm.factory import make_llm
from app.llm.types import LLMUnavailable, Message, ToolCall, ToolResult, ToolSpec


@pytest.mark.asyncio
async def test_converse_preserves_tool_round_trip():
    client = Mock()
    client.converse.return_value = {"output": {"message": {"content": [
        {"text": "Checking your account."},
        {"toolUse": {"toolUseId": "next", "name": "account", "input": {}}},
    ]}}, "stopReason": "tool_use"}
    llm = BedrockLLM("us-east-1", "model", client=client)
    turn = await llm.complete(system="system", messages=[
        Message("user", "Show my account"),
        Message("assistant", tool_calls=[ToolCall("first", "account", {})]),
        Message("tool", tool_results=[ToolResult("first", "account", {"balance": 42})]),
    ], tools=[ToolSpec("account", "Read account", {"type": "object"})])
    request = client.converse.call_args.kwargs
    assert request["messages"][1]["content"][0]["toolUse"]["toolUseId"] == "first"
    assert request["messages"][2] == {"role": "user", "content": [
        {"toolResult": {"toolUseId": "first", "content": [{"json": {"balance": 42}}]}}
    ]}
    assert request["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"] == {"type": "object"}
    assert turn.tool_calls == [ToolCall("next", "account", {})]
    assert turn.text == "Checking your account."


@pytest.mark.asyncio
async def test_access_error_does_not_expose_provider_message():
    client = Mock()
    client.converse.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "sensitive detail"}}, "Converse")
    with pytest.raises(LLMUnavailable, match="AccessDeniedException") as error:
        await BedrockLLM("us-east-1", "model", client=client).complete(
            system="system", messages=[Message("user", "hello")], tools=[])
    assert "sensitive detail" not in str(error.value)


def test_factory_selects_bedrock():
    assert isinstance(make_llm(Settings(llm_provider="bedrock"), {}), BedrockLLM)


@pytest.mark.asyncio
async def test_missing_key_fails_without_network(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    with pytest.raises(LLMUnavailable, match="AWS_BEARER_TOKEN_BEDROCK"):
        await BedrockLLM("us-east-1", "model").complete(
            system="system", messages=[Message("user", "hello")], tools=[])
