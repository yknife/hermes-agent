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
    ) -> dict[str, Any]:
        if self.api_mode == "responses":
            path = "/responses"
            payload: dict[str, Any] = {
                "model": self.model,
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
                "model": self.model,
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

        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
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
                if attempt >= self.max_retries or not retryable:
                    detail = type(exc).__name__
                    if isinstance(exc, httpx.HTTPStatusError):
                        detail = f"HTTP {exc.response.status_code}"
                    raise HermesClientError(
                        f"Hermes request failed: {detail}",
                        retryable=retryable,
                    ) from exc
                await asyncio.sleep(min(0.5 * (2**attempt), 4.0))

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
