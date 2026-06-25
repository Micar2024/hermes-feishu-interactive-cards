"""Feishu Interactive Cards plugin for Hermes.

Platform-agnostic interactive card rendering for Hermes conversations.
Hooks into Hermes plugin system (no monkey-patch).

Architecture:
  events.py       — Event schema (mirrors FEISHU_STREAMING_EVENTS)
  session.py      — ConversationState machine
  render.py       — JSON-IR rendering (platform-independent)
  adapter_feishu.py — Feishu 2.0 card adapter (IR → 飞书 schema)
  adapter_telegram.py — Telegram markdown/html adapter
  plugin.py       — Hermes plugin entry (register_hook / invoke_hook)
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["events", "session", "render", "adapter_feishu", "plugin"]

# Import register() from plugin module so Hermes finds it in __init__.py
from .plugin import register  # noqa: F401
