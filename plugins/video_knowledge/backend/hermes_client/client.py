import asyncio
import ipaddress
import json
import re
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx


def _is_loopback_url(value: str) -> bool:
    hostname = urlsplit(value).hostname
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class HermesClientError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = "HERMES_UNAVAILABLE" if retryable else "HERMES_INVALID_RESPONSE"


class HermesClientProtocol(Protocol):
    model: str

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: Mapping[str, Any],
        model: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]: ...


class HermesClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        api_mode: str = "chat_completions",
        model: str = "hermes-agent",
        timeout_seconds: float = 600.0,
        max_retries: int = 3,
        max_output_tokens: int = 4096,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_mode = api_mode
        self.model = model
        self.max_retries = max(0, max_retries)
        self.max_output_tokens = min(max(256, max_output_tokens), 16_384)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            # Windows may supply a proxy through the system registry even
            # when HTTP_PROXY is absent. The authenticated Desktop listener
            # is a loopback service and its traffic must never leave the host.
            trust_env=not _is_loopback_url(self.base_url),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: Mapping[str, Any],
        model: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        selected_model = model.strip() if model and model.strip() else self.model
        selected_provider = provider.strip() if provider and provider.strip() else None
        if self.api_mode == "responses":
            path = "/responses"
            payload: dict[str, Any] = {
                "model": selected_model,
                "instructions": system_prompt,
                "input": user_prompt,
                "max_output_tokens": self.max_output_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": dict(schema),
                        "strict": True,
                    }
                },
            }
        else:
            path = "/chat/completions"
            payload = {
                "model": selected_model,
                "max_tokens": self.max_output_tokens,
                "model_options": {
                    "structured_mode": True,
                    "reasoning": {"enabled": False},
                    "max_tokens": self.max_output_tokens,
                },
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": dict(schema),
                        "strict": True,
                    },
                },
            }
        if selected_provider is not None:
            payload["provider"] = selected_provider

        response: httpx.Response | None = None
        retry_attempt = 0
        response_format_fallback_used = False
        while True:
            try:
                response = await self._client.post(
                    f"{self.base_url}{path}", json=payload
                )
                response.raise_for_status()
                break
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.HTTPStatusError,
            ) as exc:
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and self.api_mode == "chat_completions"
                    and not response_format_fallback_used
                    and self._response_format_is_unsupported(exc.response)
                ):
                    # Some OpenAI-compatible providers expose a model in the
                    # catalog even though that model rejects json_schema while
                    # still supporting json_object. Keep provider-enforced JSON
                    # syntax and Hermes structured mode, but move the detailed
                    # schema into the trusted system message for one
                    # compatibility retry. KnowledgeService still performs
                    # strict Pydantic and citation validation afterward.
                    payload = self._schema_prompt_fallback(
                        payload,
                        schema_name=schema_name,
                        schema=schema,
                    )
                    response_format_fallback_used = True
                    continue
                retryable = not isinstance(
                    exc, httpx.HTTPStatusError
                ) or exc.response.status_code in {
                    408,
                    409,
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if retry_attempt >= self.max_retries or not retryable:
                    detail = type(exc).__name__
                    if isinstance(exc, httpx.HTTPStatusError):
                        detail = f"HTTP {exc.response.status_code}"
                    raise HermesClientError(
                        f"Hermes request failed: {detail}",
                        retryable=retryable,
                    ) from exc
                await asyncio.sleep(min(0.5 * (2**retry_attempt), 4.0))
                retry_attempt += 1

        if response is None:
            raise HermesClientError("Hermes request did not return a response")
        try:
            body = response.json()
            content = self._response_text(body)
            value = self._parse_json_object(content)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HermesClientError(
                "Hermes returned an invalid structured response", retryable=False
            ) from exc
        if not isinstance(value, dict):
            raise HermesClientError(
                "Hermes JSON response must be an object", retryable=False
            )
        return value

    @staticmethod
    def _response_format_is_unsupported(response: httpx.Response) -> bool:
        """Recognize only an explicit provider capability rejection.

        Hermes Gateway wraps an upstream provider's 400 as a 502, so the
        status code alone cannot distinguish this from a transient outage.
        The response body is inspected locally but is never included in a
        raised exception or log message.
        """

        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if (
                isinstance(error, dict)
                and error.get("code") == "response_format_unsupported"
            ):
                return True
        except (json.JSONDecodeError, UnicodeDecodeError, RuntimeError):
            pass
        try:
            detail = response.text.casefold()
        except (UnicodeDecodeError, RuntimeError):
            return False
        if "response_format" not in detail and "response format" not in detail:
            return False
        return any(
            marker in detail
            for marker in (
                "unavailable",
                "unsupported",
                "not supported",
                "does not support",
            )
        )

    @staticmethod
    def _schema_prompt_fallback(
        payload: Mapping[str, Any],
        *,
        schema_name: str,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        fallback = dict(payload)
        fallback["response_format"] = {"type": "json_object"}
        messages = [dict(item) for item in payload.get("messages", [])]
        schema_instruction = (
            "\n\nThe selected provider cannot enforce response_format. "
            "You must still return only one JSON object that conforms to the "
            f"JSON Schema named {schema_name!r}:\n"
            + json.dumps(
                dict(schema),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = (
                str(messages[0].get("content", "")) + schema_instruction
            )
        else:
            messages.insert(
                0, {"role": "system", "content": schema_instruction.lstrip()}
            )
        fallback["messages"] = messages
        return fallback

    def _response_text(self, body: Mapping[str, Any]) -> str:
        if self.api_mode == "responses":
            if isinstance(body.get("output_text"), str):
                return str(body["output_text"])
            for item in body.get("output", []):
                for content in item.get("content", []):
                    text = content.get("text")
                    if isinstance(text, str):
                        return text
            raise KeyError("output_text")
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("message content is not text")
        return content

    @staticmethod
    def _strip_json_fence(value: str) -> str:
        stripped = value.strip()
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
        return match.group(1) if match else stripped

    @classmethod
    def _parse_json_object(cls, value: str) -> Any:
        """Parse a JSON object even when a model wraps it in commentary.

        Local reasoning models commonly emit a ``<think>`` block or a short
        sentence before the requested object.  Collect every complete object
        that Python's strict JSON decoder can find and choose the widest one;
        nested objects therefore cannot displace the requested root object.
        The caller still requires a mapping and the knowledge service applies
        the requested Pydantic schema afterward.
        """
        stripped = cls._strip_json_fence(value)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as original_error:
            decoder = json.JSONDecoder()
            candidates: list[tuple[int, int, Any]] = []
            for match in re.finditer(r"\{", stripped):
                try:
                    candidate, end = decoder.raw_decode(stripped, match.start())
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    candidates.append((match.start(), end, candidate))
            if not candidates:
                raise original_error
            outermost = [
                candidate
                for candidate in candidates
                if not any(
                    other_start < candidate[0] and candidate[1] <= other_end
                    for other_start, other_end, _other_value in candidates
                )
            ]
            # Reasoning may contain valid example/schema objects before the
            # answer. Prefer the last complete top-level object while keeping
            # nested objects out of consideration.
            return max(outermost, key=lambda item: item[0])[2]
