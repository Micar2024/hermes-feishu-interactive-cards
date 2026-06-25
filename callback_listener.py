"""Feishu card action callback listener.

Uses lark-oapi's WebSocket client to receive card button click events.
Why WebSocket instead of HTTP webhook?
- AGENTS.md forbids touching gateway core files
- We don't want to expose a public endpoint
- WebSocket is push-based — Feishu connects to us, not the other way
- The SDK handles reconnect, auth, signing automatically

For v0.3: implements the "A" path (record + toast, no LLM continuation).
The "B" path (auto-send text → trigger LLM continuation) needs a hook
into the gateway's incoming message handler, which is a v0.4 task.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Imported at module load time. The SDK is required for the listener
# to function; if it's missing, the plugin should fail loudly at
# register-time (not at first card-send time).
try:
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )
    _LARK_SDK_AVAILABLE = True
except ImportError:
    _LARK_SDK_AVAILABLE = False
    P2CardActionTrigger = None
    P2CardActionTriggerResponse = None


# Global singleton so the plugin can introspect / stop it
_listener_thread: Optional[threading.Thread] = None
_stop_flag: threading.Event = threading.Event()


def _build_card_action_handler(on_button_click):
    """Build a P2CardActionTrigger handler function.

    Public-ish (underscore prefix is just convention here) so tests
    can call it directly without going through WebSocket. Returns a
    closure that captures the on_button_click callable.
    """
    def do_card_action_trigger(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            payload = {
                "event_id": getattr(data, "event_id", ""),
                "tenant_key": getattr(data, "tenant_key", ""),
                "open_id": getattr(data, "open_id", ""),
                "user_id": getattr(data, "user_id", ""),
                "action": {},
                "context": {},
            }
            # The Feishu WS protocol puts the real payload under .event
            ev = getattr(data, "event", None)
            if ev is not None:
                if hasattr(ev, "action") and ev.action is not None:
                    payload["action"] = {
                        "value": getattr(ev.action, "value", {}),
                        "tag": getattr(ev.action, "tag", ""),
                    }
                if hasattr(ev, "context") and ev.context is not None:
                    # v0.3 fix: real field names from lark-oapi SDK are
                    # `open_chat_id` and `open_message_id`, NOT `chat_id` /
                    # `message_id`. Verified against:
                    #   lark_oapi.event.callback.model.p2_card_action_trigger.CallBackContext._types
                    ctx = ev.context
                    payload["context"] = {
                        "url": getattr(ctx, "url", ""),
                        "open_chat_id": getattr(ctx, "open_chat_id", ""),
                        "open_message_id": getattr(ctx, "open_message_id", ""),
                        "preview_token": getattr(ctx, "preview_token", ""),
                    }

            logger.info(
                "[feishu-interactive-cards] Card action received: %s",
                json.dumps(payload, ensure_ascii=False)[:300],
            )

            if on_button_click is not None:
                try:
                    on_button_click(payload)
                except Exception as exc:
                    logger.exception(
                        "[feishu-interactive-cards] on_button_click raised: %s", exc
                    )

            button_value = payload.get("action", {}).get("value", {})
            label = ""
            if isinstance(button_value, dict):
                label = button_value.get("button_key", button_value.get("label", ""))
            if not label:
                label = "操作"

            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "success",
                    "content": f"已收到: {label}",
                }
            })
        except Exception as exc:
            logger.exception("[feishu-interactive-cards] Handler exception: %s", exc)
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "error",
                    "content": "处理失败，请重试",
                }
            })

    return do_card_action_trigger


def start_listener(app_id: str, app_secret: str, encrypt_key: str = "",
                    verification_token: str = "",
                    on_button_click: Optional[Any] = None) -> bool:
    """Start the WebSocket listener in a background thread.

    Args:
        app_id: Feishu app ID
        app_secret: Feishu app secret
        encrypt_key: from developer console, for signature verification
        verification_token: from developer console, for event origin check
        on_button_click: callable(payload: dict) called on each card.action.trigger.
            If None, defaults to a no-op + toast (record-and-acknowledge mode).

    Returns:
        True if thread started, False if already running or import failed.
    """
    global _listener_thread

    if _listener_thread is not None and _listener_thread.is_alive():
        logger.warning("[feishu-interactive-cards] Listener already running, skipping start")
        return False

    if not _LARK_SDK_AVAILABLE:
        logger.error(
            "[feishu-interactive-cards] lark_oapi not available; cannot start listener"
        )
        return False

    import lark_oapi as lark  # lark.ws.Client is the only thing we still lazy-import

    _stop_flag.clear()

    # Build the handler closure (exposed for direct testing too)
    do_card_action_trigger = _build_card_action_handler(on_button_click)

    # Build the event dispatcher — this is what verifies signatures
    # and dispatches to our handler
    event_handler = (
        lark.EventDispatcherHandler.builder(encrypt_key, verification_token)
        .register_p2_card_action_trigger(do_card_action_trigger)
        .build()
    )

    def _run_listener():
        try:
            logger.info(
                "[feishu-interactive-cards] Starting WebSocket listener "
                "(app_id=%s...)", app_id[:10],
            )
            cli = lark.ws.Client(
                app_id, app_secret,
                event_handler=event_handler,
                domain="https://open.feishu.cn",
            )
            # lark.ws.Client.start() blocks. It auto-reconnects internally.
            # We run it until _stop_flag is set.
            while not _stop_flag.is_set():
                try:
                    cli.start()
                except Exception as exc:
                    logger.warning(
                        "[feishu-interactive-cards] WS client exited: %s. "
                        "Will retry in 5s.", exc,
                    )
                    if _stop_flag.wait(timeout=5):
                        break
        except Exception as exc:
            logger.exception("[feishu-interactive-cards] Listener thread crashed: %s", exc)
        finally:
            logger.info("[feishu-interactive-cards] Listener thread stopped")

    _listener_thread = threading.Thread(
        target=_run_listener, name="feishu-card-listener", daemon=True,
    )
    _listener_thread.start()
    return True


def stop_listener(timeout: float = 3.0) -> bool:
    """Stop the listener thread. Returns True if it was running."""
    global _listener_thread
    if _listener_thread is None or not _listener_thread.is_alive():
        return False
    _stop_flag.set()
    _listener_thread.join(timeout=timeout)
    _listener_thread = None
    return True


def is_running() -> bool:
    """Whether the WebSocket listener thread is alive."""
    return _listener_thread is not None and _listener_thread.is_alive()
