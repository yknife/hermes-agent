import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from plugins.video_knowledge.backend.hermes_client import (
    HermesClient,
    HermesClientError,
)
from plugins.video_knowledge.backend.hermes_client.client import _is_loopback_url


@pytest.mark.asyncio
async def test_reasoning_compatibility_retry_is_bounded():
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            502, json={"error": {"code": "reasoning_disabled_unsupported"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HermesClient(
            "http://hermes.test/v1", client=http_client, max_retries=0
        )
        with pytest.raises(HermesClientError):
            await client.generate_json(
                system_prompt="Return JSON",
                user_prompt="Analyze",
                schema_name="analysis",
                schema={"type": "object"},
            )
    assert len(requests) == 2
    assert requests[0]["model_options"]["reasoning"] == {"enabled": False}
    assert requests[1]["model_options"]["reasoning"] == {
        "enabled": True,
        "effort": "low",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_rejected", [False, True])
async def test_gateway_error_in_reply_triggers_real_json_object_retry(
    reasoning_rejected,
):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    requests = []
    statuses = []

    async def handle(request):
        requests.append(await request.json())
        response = await adapter._handle_chat_completions(request)
        statuses.append(response.status)
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    error = (
        "HTTP 400: The parameter `response_format.type` is not valid: "
        "`json_schema` is not supported by this model."
    )
    failure = {
        "final_response": error,
        "error": error,
        "failed": True,
        "completed": False,
    }
    success = {"final_response": '{"summary":"ok"}', "completed": True}
    results = [(failure, {})]
    if reasoning_rejected:
        reasoning_error = (
            "HTTP 400: reasoning_effort `none` is not supported by this model"
        )
        results.append((
            {
                **failure,
                "final_response": reasoning_error,
                "error": reasoning_error,
            },
            {},
        ))
    results.append((success, {}))
    with patch.object(
        adapter,
        "_run_agent",
        new=AsyncMock(side_effect=results),
    ):
        async with TestServer(app) as server:
            client = HermesClient(str(server.make_url("/v1")), max_retries=0)
            try:
                result = await client.generate_json(
                    system_prompt="Return a JSON analysis.",
                    user_prompt="Analyze this transcript.",
                    schema_name="analysis",
                    schema={
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                        "required": ["summary"],
                    },
                )
            finally:
                await client.close()

    assert result == {"summary": "ok"}
    assert statuses == ([502, 502, 200] if reasoning_rejected else [502, 200])
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[1]["response_format"] == {"type": "json_object"}
    assert requests[1]["model_options"]["structured_mode"] is True
    assert '"required":["summary"]' in requests[1]["messages"][0]["content"]
    if reasoning_rejected:
        assert requests[2]["response_format"] == {"type": "json_object"}
        assert requests[2]["model_options"]["reasoning"] == {
            "enabled": True,
            "effort": "low",
        }
        assert requests[2]["max_tokens"] == requests[0]["max_tokens"]
        assert requests[2]["model_options"]["structured_mode"] is True


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
