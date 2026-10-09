"""Bedrock Converse adapter for the existing TradeDesk copilot."""

import asyncio
import os

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.llm.types import LLMUnavailable, LLMTurn, Message, ToolCall, ToolSpec


class BedrockLLM:
    def __init__(self, region: str, model_id: str, *, client=None):
        self._region = region
        self._model_id = model_id
        self._client = client

    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        # boto3 is synchronous; run network calls off the API event loop.
        return await asyncio.to_thread(self._complete, system, messages, tools)

    def _complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        try:
            if self._client is None:
                if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip():
                    raise LLMUnavailable("Set AWS_BEARER_TOKEN_BEDROCK in the backend .env file.")
                self._client = boto3.client(
                    "bedrock-runtime", region_name=self._region,
                    config=Config(connect_timeout=10, read_timeout=60,
                                  retries={"mode": "standard", "total_max_attempts": 3}),
                )
            converted = []
            for message in messages:
                content = []
                if message.text:
                    content.append({"text": message.text})
                content.extend({"toolUse": {"toolUseId": call.id, "name": call.name,
                                            "input": call.input}} for call in message.tool_calls)
                content.extend({"toolResult": {"toolUseId": result.call_id,
                                               "content": [{"json": result.output}]}}
                               for result in message.tool_results)
                role = "user" if message.role == "tool" else message.role
                if content:
                    if converted and converted[-1]["role"] == role:
                        converted[-1]["content"].extend(content)
                    else:
                        converted.append({"role": role, "content": content})
            request = {"modelId": self._model_id, "messages": converted,
                       "system": [{"text": system}], "inferenceConfig": {"maxTokens": 800}}
            if tools:
                request["toolConfig"] = {"tools": [
                    {"toolSpec": {"name": tool.name, "description": tool.description,
                                  "inputSchema": {"json": tool.input_schema}}} for tool in tools
                ]}
            response = self._client.converse(**request)
            if response.get("stopReason") in {"guardrail_intervened", "content_filtered", "max_tokens"}:
                raise LLMUnavailable("Bedrock could not complete the response.")
            blocks = response["output"]["message"]["content"]
            calls = [ToolCall(b["toolUse"]["toolUseId"], b["toolUse"]["name"],
                              b["toolUse"]["input"]) for b in blocks if "toolUse" in b]
            return LLMTurn(text="\n".join(b["text"] for b in blocks if "text" in b), tool_calls=calls)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "UnknownError")
            raise LLMUnavailable(f"Bedrock request failed ({code}); check region, model access and key.") from None
        except BotoCoreError:
            raise LLMUnavailable("Could not connect to Bedrock; check backend configuration and network.") from None
