"""Gateway-owned transport bridge for Video Knowledge notifications."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gateway.config import Platform
from gateway.platforms.base import classify_send_error

from plugins.video_knowledge.backend.app.integration.runtime import runtime_registry
from plugins.video_knowledge.backend.app.services.notification_dispatcher import (
    DeliveryResult,
    NotificationPart,
    NotificationTarget,
)

logger = logging.getLogger(__name__)


class GatewayNotificationTransport:
    """Routes through the already-connected adapter owned by GatewayRunner."""

    def __init__(
        self,
        adapter_getter: Callable[[], Any | None],
        home_getter: Callable[[], Any | None],
    ) -> None:
        self._adapter_getter = adapter_getter
        self._home_getter = home_getter

    def available(self, platform: str) -> bool:
        return platform == "feishu" and self._adapter_getter() is not None

    async def deliver(
        self,
        target: NotificationTarget,
        part: NotificationPart,
    ) -> DeliveryResult:
        adapter = self._adapter_getter()
        if adapter is None:
            return DeliveryResult(False, "PLATFORM_UNAVAILABLE", True)
        metadata = {
            "reply_to_message_id": target.reply_to_message_id,
            "notification_idempotency_key": part.idempotency_key,
        }
        if target.thread_id:
            metadata["thread_id"] = target.thread_id
        try:
            result = await adapter.send(
                chat_id=target.chat_id,
                content=part.content,
                reply_to=target.reply_to_message_id,
                metadata=metadata,
            )
        except Exception as exc:
            kind = classify_send_error(exc)
            return self._failure(kind)
        if result.success:
            return DeliveryResult(True)
        response_code = getattr(result.raw_response, "code", None)
        kind = (
            "not_found"
            if response_code in {230011, 231003}
            else result.error_kind or classify_send_error(None, result.error or "")
        )
        return self._failure(kind, retryable=bool(result.retryable))

    async def alert(self, *, content: str, idempotency_key: str) -> None:
        adapter = self._adapter_getter()
        home = self._home_getter()
        if adapter is None or home is None or not getattr(home, "chat_id", None):
            return
        metadata = {"notification_idempotency_key": idempotency_key}
        thread_id = getattr(home, "thread_id", None)
        if thread_id:
            metadata["thread_id"] = str(thread_id)
        try:
            await adapter.send(
                chat_id=str(home.chat_id),
                content=content,
                metadata=metadata,
            )
        except Exception:
            logger.warning("Video Knowledge operational alert could not be delivered")

    @staticmethod
    def _failure(kind: str, *, retryable: bool = False) -> DeliveryResult:
        if kind == "forbidden":
            return DeliveryResult(False, "AUTHORIZATION_FAILED", False)
        if kind == "not_found":
            return DeliveryResult(False, "INVALID_TARGET", False)
        if kind == "rate_limited":
            return DeliveryResult(False, "FEISHU_RATE_LIMITED", True)
        if kind == "transient":
            return DeliveryResult(False, "FEISHU_TRANSIENT", True)
        if kind == "bad_format":
            return DeliveryResult(False, "DELIVERY_FORMAT_ERROR", False)
        return DeliveryResult(False, "DELIVERY_ERROR", retryable or kind == "unknown")


class GatewayNotificationRuntimeManager:
    """Starts one transport-neutral dispatcher for each Gateway-served profile."""

    def __init__(self) -> None:
        self._profile_homes: list[Path] = []
        self.started_count = 0

    async def start(self, runner: Any) -> int:
        from hermes_cli.profiles import profiles_to_serve

        multiplex = bool(getattr(runner.config, "multiplex_profiles", False))
        allowlist = getattr(runner.config, "multiplex_profile_allowlist", None)
        profiles = profiles_to_serve(multiplex, allowlist)
        active_name = runner._active_profile_name()
        started = 0
        for profile_name, profile_home in profiles:
            profile_home = Path(profile_home)
            database_path = profile_home / "video-knowledge" / "data" / "app.db"
            if not await asyncio.to_thread(database_path.is_file):
                continue
            adapter_map = (
                runner.adapters
                if profile_name == active_name
                else runner._profile_adapters.get(profile_name, {})
            )

            def adapter_getter(adapter_map=adapter_map):
                return adapter_map.get(Platform.FEISHU)

            def home_getter(profile_name=profile_name, profile_home=profile_home):
                if profile_name == active_name:
                    config = runner.config
                else:
                    from gateway.config import load_gateway_config
                    from gateway.run import _profile_runtime_scope

                    with _profile_runtime_scope(profile_home):
                        config = load_gateway_config()
                platform_config = config.platforms.get(Platform.FEISHU)
                return platform_config.home_channel if platform_config else None

            transport = GatewayNotificationTransport(adapter_getter, home_getter)
            runtime = await runtime_registry.get(
                profile_home,
                gateway_base_url=os.getenv(
                    "HERMES_GATEWAY_URL", "http://127.0.0.1:8642"
                ),
                gateway_api_key=None,
                start_worker=False,
                notification_transport=transport,
            )
            if runtime.notification_dispatcher is not None:
                self._profile_homes.append(profile_home)
                started += 1
        if started:
            logger.info(
                "Video Knowledge notification dispatchers started for %d profile(s)",
                started,
            )
        self.started_count = started
        return started

    async def stop(self) -> None:
        homes, self._profile_homes = self._profile_homes, []
        self.started_count = 0
        for profile_home in homes:
            await runtime_registry.stop(profile_home)


async def start_gateway_notification_runtimes(
    runner: Any,
) -> GatewayNotificationRuntimeManager:
    manager = GatewayNotificationRuntimeManager()
    await manager.start(runner)
    return manager
