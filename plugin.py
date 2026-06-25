"""Hermes plugin entry point.

Registers hooks into the Hermes plugin system.
No monkey-patching, no sidecar, no HTTP server.

For each Hermes hook, we:
1. Build an event from the hook payload
2. Update the CardPipeline state machine
3. If state changed AND this is Feishu, send/edit the card via lark-oapi SDK
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy imports to avoid loading heavy deps at plugin init
_pipelines: Dict[str, "CardPipeline"] = {}


def register(ctx):
    """Register all hooks with the Hermes plugin system.

    Called by Hermes PluginManager with a PluginContext.
    """
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_approval_request", _on_pre_approval_request)
    ctx.register_hook("post_approval_response", _on_post_approval_response)

    # v0.3 #4: start the card action callback listener. It runs as
    # a daemon thread using lark-oapi's WebSocket client — no public
    # endpoint required. The handler routes button clicks to the
    # matching pipeline (look up by chat_id from the event payload).
    _start_card_action_listener()

    logger.info("[feishu-interactive-cards] Plugin registered with Hermes")


def _start_card_action_listener():
    """Start the Feishu card action WebSocket listener if creds are configured.

    Failure modes are silent (logged) — the plugin can still send cards
    even if the listener never connects. Listener is needed only for
    receiving button clicks.
    """
    try:
        import yaml
        config_path = Path.home() / ".hermes" / "config.yaml"
        if not config_path.exists():
            logger.info("[feishu-interactive-cards] config.yaml missing; listener not started")
            return
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        feishu_cfg = cfg.get("feishu", {})
        app_id = feishu_cfg.get("app_id")
        app_secret = feishu_cfg.get("app_secret")
        if not app_id or not app_secret:
            logger.info("[feishu-interactive-cards] feishu creds missing; listener not started")
            return
        # v0.3: use the same encrypt_key/verification_token from the
        # developer console. Currently the gateway doesn't expose these
        # in config.yaml, so we pass empty strings and rely on the SDK
        # doing no signature verification (still receives events).
        # Production: pull these from cfg["feishu"]["encrypt_key"] etc.
        from .callback_listener import start_listener
        started = start_listener(
            app_id=app_id,
            app_secret=app_secret,
            on_button_click=_on_card_button_clicked,
        )
        if started:
            logger.info("[feishu-interactive-cards] Card action listener started")
        else:
            logger.info("[feishu-interactive-cards] Card action listener already running or disabled")
    except Exception as exc:
        logger.exception("[feishu-interactive-cards] Failed to start listener: %s", exc)


def _on_card_button_clicked(payload: Dict[str, Any]) -> None:
    """Route a card button click to the matching pipeline.

    v0.3 #4 — "A path": record the click on the IR (the user sees
    feedback when the card re-renders). Does NOT auto-continue the
    LLM conversation — that's the v0.4 "B path" task.
    """
    try:
        from .session import EventType
        from .events import build_event

        open_chat_id = payload.get("context", {}).get("open_chat_id", "")
        action_value = payload.get("action", {}).get("value", {})

        # Find the pipeline for this chat. Pipelines are keyed
        # {session_id}:{platform} — chat_id == session_id in our model.
        pipeline = _pipelines.get(f"{open_chat_id}:feishu")
        if pipeline is None:
            logger.info(
                "[feishu-interactive-cards] Button click for unknown chat %s; ignoring",
                open_chat_id,
            )
            return

        # Real Feishu Card 2.0 callback payload:
        #   action.value.action = "approve"   (we set this in adapter_feishu._render_buttons)
        #   action.name        = "Button_xxxx" (auto-generated button name)
        # Fall back to action.tag if value is empty.
        button_key = (
            action_value.get("action")
            if isinstance(action_value, dict) and action_value.get("action")
            else payload.get("action", {}).get("tag", "unknown")
        )
        logger.info(
            "[feishu-interactive-cards] Button click recorded: chat=%s key=%s",
            open_chat_id, button_key,
        )

        ev = build_event(
            EventType.INTERACTION_COMPLETED,
            session_id=open_chat_id,
            turn_id=pipeline.turn_id or "",
            button_key=button_key,
            button_value=action_value,
        )
        pipeline.process_event(ev)
        # Push a card update so the user sees the click registered
        # (re-uses the existing edit path). `platform` lives on the
        # pipeline (CardPipeline.platform), not on the IR.
        if pipeline.ir.message_key and pipeline.platform == "feishu":
            _edit_card_async(pipeline, open_chat_id, "feishu", final=False)
        else:
            logger.warning(
                "[feishu-interactive-cards] Skipped edit: message_key=%r platform=%r",
                pipeline.ir.message_key, pipeline.platform,
            )
    except Exception as exc:
        logger.exception("[feishu-interactive-cards] Button click handler failed: %s", exc)


def _get_or_create_pipeline(session_id: str, platform: str = "") -> "CardPipeline":
    """Get or create a CardPipeline for this session."""
    from .session import CardPipeline

    key = f"{session_id}:{platform}"
    if key not in _pipelines:
        _pipelines[key] = CardPipeline(session_id, platform)
    return _pipelines[key]


def _cleanup_pipeline(session_id: str, platform: str = ""):
    """Remove pipeline after session ends."""
    key = f"{session_id}:{platform}"
    _pipelines.pop(key, None)


def _extract_source_info(event_obj) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (chat_id, platform, session_id) from MessageEvent.

    Returns (None, None, None) if extraction fails.
    """
    try:
        # MessageEvent has .source (SessionSource) with .chat_id, .platform
        source = getattr(event_obj, "source", None)
        if source is None:
            return None, None, None
        chat_id = getattr(source, "chat_id", None)
        platform = getattr(source, "platform", None)
        # platform might be a string or enum
        if hasattr(platform, "value"):
            platform = platform.value
        session_id = getattr(source, "session_key", None) or getattr(source, "chat_id", None)
        return chat_id, platform, session_id
    except Exception as exc:
        logger.debug("[feishu-interactive-cards] extract_source_info failed: %s", exc)
        return None, None, None


def _schedule_card_send(coro):
    """Schedule a coroutine to run on the running event loop.

    Falls back to running it in a new loop if no loop is running.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running loop — run in new loop (shouldn't happen in gateway)
        try:
            asyncio.run(coro)
        except Exception as exc:
            logger.error("[feishu-interactive-cards] Card send failed: %s", exc)


# ===========================================================================
# Hook implementations
# ===========================================================================

def _on_pre_gateway_dispatch(event=None, **kwargs):
    """Intercept incoming messages to create initial card.

    Hook fires once per incoming MessageEvent. We:
    1. Create a CardPipeline for this session
    2. Send initial card (empty body, just title + status)
    3. Store message_id on pipeline for later edits
    """
    from .session import EventType
    from .events import build_event

    chat_id, platform, session_id = _extract_source_info(event)
    if not chat_id or not platform:
        # Not a platform message we care about
        return None

    if platform != "feishu":
        # Only Feishu for now
        return None

    user_text = getattr(event, "text", "") if event else ""

    pipeline = _get_or_create_pipeline(session_id, platform)
    ev = build_event(EventType.SESSION_START,
                     session_id=session_id,
                     platform=platform,
                     model=kwargs.get("model", ""),
                     user_message=user_text)
    ir = pipeline.process_event(ev)
    if ir is None:
        return None

    # Send initial card asynchronously
    from .adapter_feishu import render_feishu_card
    from .feishu_sender import get_feishu_client

    client = get_feishu_client()
    if client is None:
        logger.debug("[feishu-interactive-cards] No client, skipping send")
        return None

    card = render_feishu_card(ir)
    pipeline.ir.message_key = ""  # will be filled in on response

    async def _send():
        try:
            success, msg_id = await client.send_card(
                receive_id=chat_id,
                receive_id_type="chat_id",
                card_payload=card,
            )
            if success:
                pipeline.ir.message_key = msg_id
                logger.info(
                    "[feishu-interactive-cards] Initial card sent: %s chat=%s",
                    msg_id, chat_id,
                )
            else:
                logger.warning(
                    "[feishu-interactive-cards] Initial card send failed: %s",
                    msg_id,
                )
        except Exception as exc:
            logger.exception("[feishu-interactive-cards] Send exception: %s", exc)

    _schedule_card_send(_send())
    return None  # don't modify event


def _on_post_tool_call(tool_name: str, args: Dict, result: Any,
                       session_id: str, task_id: str, tool_call_id: str,
                       duration_ms: int = 0, platform: str = "", **kwargs):
    """Update card with tool progress.

    Hook fires after each tool call completes. We:
    1. Update CardPipeline state
    2. If Feishu and pipeline has message_key, edit the card
    """
    from .session import EventType
    from .events import build_event

    if platform != "feishu":
        return

    pipeline = _get_or_create_pipeline(session_id, platform)

    # Emit tool start if first time seeing this tool
    if tool_name not in pipeline.ir.tool_states:
        ev = build_event(EventType.TOOL_START,
                         session_id=session_id,
                         turn_id=task_id,
                         tool_name=tool_name,
                         tool_args=dict(args),
                         tool_call_id=tool_call_id)
        pipeline.process_event(ev)

    # Extract error info from result
    is_error = False
    error_message = ""
    result_str = ""
    if isinstance(result, dict):
        if result.get("error"):
            is_error = True
            error_message = str(result.get("error", ""))[:200]
            result_str = error_message
        elif result.get("blocked"):
            is_error = True
            result_str = "blocked by approval"
        else:
            result_str = str(result)[:200]
    else:
        result_str = str(result)[:200]

    ev = build_event(EventType.TOOL_END,
                     session_id=session_id,
                     turn_id=task_id,
                     tool_name=tool_name,
                     tool_call_id=tool_call_id,
                     result_summary=result_str,
                     status="error" if is_error else "success",
                     duration_ms=duration_ms)
    pipeline.process_event(ev)

    # Edit card if we have a message_key
    msg_key = pipeline.ir.message_key
    if not msg_key:
        return  # initial card not yet sent

    chat_id = kwargs.get("chat_id", "")
    _edit_card_async(pipeline, chat_id=chat_id, platform=platform)


def _on_transform_llm_output(response_text: str, session_id: str,
                             platform: str, model: str, **kwargs) -> Optional[str]:
    """Transform response through card pipeline.

    For v0.2: we don't modify the text (Hermes' final answer goes out as
    a normal text message). We just update card state with the answer.
    """
    from .session import EventType
    from .events import build_event

    if platform != "feishu":
        return None  # pass through

    pipeline = _get_or_create_pipeline(session_id, platform)

    ev = build_event(EventType.ANSWER_DELTA,
                     session_id=session_id,
                     turn_id=kwargs.get("turn_id", ""),
                     content=response_text,
                     is_final=True)
    pipeline.process_event(ev)

    msg_key = pipeline.ir.message_key
    if msg_key:
        _edit_card_async(pipeline, chat_id=kwargs.get("chat_id", ""), platform=platform)

    # Don't modify the response text — let it go out as normal text
    return None


def _on_post_llm_call(session_id: str, task_id: str, turn_id: str,
                      user_message: str, assistant_response: str,
                      model: str, platform: str, **kwargs):
    """Finalize card on LLM call completion."""
    from .session import EventType
    from .events import build_event

    if platform != "feishu":
        return

    pipeline = _get_or_create_pipeline(session_id, platform)

    ev = build_event(EventType.ANSWER_END,
                     session_id=session_id,
                     turn_id=turn_id,
                     total_chars=len(assistant_response or ""))
    pipeline.process_event(ev)

    msg_key = pipeline.ir.message_key
    if msg_key:
        _edit_card_async(pipeline, chat_id=kwargs.get("chat_id", ""), platform=platform)


def _on_session_start(session_id: str, model: str, platform: str, **kwargs):
    """Clean slate for new session."""
    _cleanup_pipeline(session_id, platform)


def _on_session_end(session_id: str, task_id: str, turn_id: str,
                    completed: bool, interrupted: bool, model: str,
                    platform: str, **kwargs):
    """Close out session card."""
    from .session import EventType
    from .events import build_event

    pipeline = _get_or_create_pipeline(session_id, platform)

    ev = build_event(EventType.SESSION_END,
                     session_id=session_id,
                     completed=completed,
                     interrupted=interrupted,
                     model=model)
    pipeline.process_event(ev)

    # Final card update
    if platform == "feishu" and pipeline.ir.message_key:
        _edit_card_async(pipeline, chat_id=kwargs.get("chat_id", ""), platform=platform,
                         final=True)

    _cleanup_pipeline(session_id, platform)


def _on_pre_approval_request(session_id: str, turn_id: str, tool_call_id: str,
                             buttons: List[Dict], **kwargs):
    """Handle approval/choice buttons.

    v0.3: write buttons into pipeline.ir.interaction_buttons so the
    adapter actually renders them. (v0.2 wired the event but never
    wrote the buttons to the IR — so they never showed up in the card.)
    """
    from .session import EventType
    from .events import build_event

    platform = kwargs.get("platform", "")
    if platform != "feishu":
        return

    pipeline = _get_or_create_pipeline(session_id, platform)

    # v0.3 fix: write buttons to the IR so adapter renders them
    pipeline.ir.interaction_buttons = buttons or []

    ev = build_event(EventType.INTERACTION_REQUESTED,
                     session_id=session_id,
                     turn_id=turn_id,
                     message_type="approval",
                     buttons=buttons)
    pipeline.process_event(ev)


def _on_post_approval_response(session_id: str, turn_id: str, tool_call_id: str,
                               button_key: str, button_value: Any, **kwargs):
    """Handle approval/choice response."""
    from .session import EventType
    from .events import build_event

    platform = kwargs.get("platform", "")
    if platform != "feishu":
        return

    pipeline = _get_or_create_pipeline(session_id, platform)

    ev = build_event(EventType.INTERACTION_COMPLETED,
                     session_id=session_id,
                     turn_id=turn_id,
                     button_key=button_key,
                     button_value=button_value)
    pipeline.process_event(ev)


# ===========================================================================
# Card sending helpers
# ===========================================================================

def _edit_card_async(pipeline, chat_id: str, platform: str, final: bool = False):
    """Asynchronously edit the card in place.

    Falls back to sending a new card if message_key is missing.
    """
    from .adapter_feishu import render_feishu_card
    from .feishu_sender import get_feishu_client

    client = get_feishu_client()
    if client is None:
        return

    msg_key = pipeline.ir.message_key
    card = render_feishu_card(pipeline.ir)

    async def _do():
        try:
            if msg_key:
                # Edit existing card
                success, result = await client.edit_card(msg_key, card)
                if success:
                    # v0.3 fix: track edit count for footer display
                    pipeline.ir.edit_count += 1
                if not success:
                    logger.debug(
                        "[feishu-interactive-cards] Edit failed (%s), card state may be stale",
                        result,
                    )
            elif chat_id:
                # No message_key yet — send as new
                success, msg_id = await client.send_card(
                    receive_id=chat_id,
                    receive_id_type="chat_id",
                    card_payload=card,
                )
                if success:
                    pipeline.ir.message_key = msg_id
                    pipeline.ir.edit_count += 1
        except Exception as exc:
            logger.debug("[feishu-interactive-cards] Edit exception: %s", exc)

    _schedule_card_send(_do())
