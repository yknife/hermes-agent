"""Bundled Video Knowledge plugin for Hermes Agent."""

from __future__ import annotations

from plugins.video_knowledge.tools import TOOLS


def register(ctx) -> None:
    """Expose profile-scoped, read-only knowledge tools to Hermes chat."""
    for name, schema, handler in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="video_knowledge",
            schema=schema,
            handler=handler,
            is_async=True,
            description="Read-only access to the current Hermes profile's video knowledge.",
            emoji="🎬",
        )
