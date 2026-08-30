import json

import httpx
import pytest
from plugins.video_knowledge.backend.hermes_client import (
    HermesClient,
    HermesClientError,
)
from plugins.video_knowledge.backend.hermes_client.client import _is_loopback_url


def test_loopback_urls_bypass_system_proxy_detection() -> None:
    assert _is_loopback_url("http://127.0.0.1:8642/v1")
    assert _is_loopback_url("http://[::1]:8642/v1")
    assert _is_loopback_url("http://localhost:8642/v1")
    assert not _is_loopback_url("https://hermes.example.com/v1")


def test_parser_extracts_final_json_object_from_reasoning_wrapper() -> None:
    content = (
        '<think>先确认目标结构 {"example": true}</think>\n'
        '结果如下：\n```json\n{"summary":"ok","nested":{"value":1}}\n```\n完成。'
    )

    assert HermesClient._parse_json_object(content) == {
        "summary": "ok",
        "nested": {"value": 1},
    }


def test_parser_prefers_final_top_level_object_over_larger_reasoning_json() -> None:
    content = (
        '<think>{"schema":{"properties":{"summary":{"type":"string"}}}}</think>\n'
        '{"summary":"final"}'
    )

    assert HermesClient._parse_json_object(content) == {"summary": "final"}


@pytest.mark.asyncio
async def test_chat_completions_returns_structured_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["model_options"]["structured_mode"] is True
        assert payload["model_options"]["reasoning"] == {"enabled": False}
        assert payload["model_options"]["max_tokens"] == 4096
        assert payload["max_tokens"] == 4096
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '```json\n{"summary":"ok"}\n```'}}]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HermesClient("http://hermes.test/v1", client=http_client)
        result = await client.generate_json(
            system_prompt="system",
            user_prompt="user",
            schema_name="analysis",
            schema={"type": "object"},
        )
    assert result == {"summary": "ok"}


@pytest.mark.asyncio
async def test_chat_completions_applies_request_scoped_model_without_changing_default() -> (
    None
):
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3.5-4b"
        assert payload["provider"] == "custom:ynknife_local"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"summary":"ok"}'}}]}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HermesClient(
            "http://hermes.test/v1", model="hermes-agent", client=http_client
        )
        result = await client.generate_json(
            system_prompt="system",
            user_prompt="user",
            schema_name="analysis",
            schema={"type": "object"},
            model="qwen3.5-4b",
            provider="custom:ynknife_local",
        )

    assert result == {"summary": "ok"}
    assert client.model == "hermes-agent"


@pytest.mark.asyncio
async def test_chat_completions_retries_with_schema_prompt_when_format_is_unsupported() -> (
    None
):
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                502,
                json={
                    "error": {
                        "message": "Upstream provider rejected the request",
                        "type": "server_error",
                        "code": "response_format_unsupported",
                    }
                },
            )
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["model_options"]["structured_mode"] is True
        assert "JSON Schema named 'analysis'" in payload["messages"][0]["content"]
        assert '"required":["summary"]' in payload["messages"][0]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"summary":"ok"}'}}]},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HermesClient(
            "http://hermes.test/v1",
            client=http_client,
            max_retries=0,
        )
        result = await client.generate_json(
            system_prompt="system",
            user_prompt="user",
            schema_name="analysis",
            schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
        )

    assert result == {"summary": "ok"}
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_unrelated_gateway_failure_does_not_disable_response_format() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            502,
            json={"error": {"message": "upstream connection failed"}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HermesClient(
            "http://hermes.test/v1",
            client=http_client,
            max_retries=0,
        )
        with pytest.raises(HermesClientError):
            await client.generate_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="analysis",
                schema={"type": "object"},
            )

    assert len(requests) == 1
    assert requests[0]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_invalid_response_is_not_retryable() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"choices": [{"message": {"content": "not-json"}}]}
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HermesClient("http://hermes.test/v1", client=http_client)
        with pytest.raises(HermesClientError) as captured:
            await client.generate_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="analysis",
                schema={"type": "object"},
            )
    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_http_failure_reports_status_without_response_body() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            404, json={"error": "sensitive upstream response must not leak"}
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HermesClient("http://hermes.test/v1", client=http_client)
        with pytest.raises(HermesClientError) as captured:
            await client.generate_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="analysis",
                schema={"type": "object"},
            )

    assert str(captured.value) == "Hermes request failed: HTTP 404"
    assert "sensitive" not in str(captured.value)
    assert captured.value.retryable is False
