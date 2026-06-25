"""Render IR to platform-specific card schemas.

Currently supports Feishu 2.0 interactive cards.
"""

from __future__ import annotations

import html
import time
from typing import Any, Dict, List, Optional

from .session import CardIR, CardSection


# ---------------------------------------------------------------------------
# Feishu 2.0 card schema renderer
# ---------------------------------------------------------------------------

FEISHU_VARIANTS = {
    "primary": "blue",
    "warning": "orange",
    "danger": "red",
    "neutral": "default",
}


def render_feishu_card(ir: CardIR) -> Dict[str, Any]:
    """Render a CardIR into a Feishu 2.0 card payload.

    Returns the card JSON dict (without msg_type/content wrapping).
    Callers wrap with `{"msg_type": "interactive", "content": json.dumps(card)}`
    when calling the API.

    Schema reference (Feishu 2.0 interactive cards):
      {header: {template, title}, elements: [...], footer: {...}}

    NOT nested under body — that's the v1 schema. v2 uses top-level elements.
    """
    return _build_card_body(ir)


def _build_card_body(ir: CardIR) -> Dict[str, Any]:
    """Build the full card JSON structure (Feishu 2.0 flat schema)."""
    card: Dict[str, Any] = {}

    header = _build_header(ir)
    if header:
        card["header"] = header

    # v2 schema: elements at top level, not nested under body
    body_elements = _build_body_elements(ir)
    if body_elements:
        card["elements"] = body_elements

    footer = _build_footer(ir)
    if footer:
        card["footer"] = footer

    return card


def _build_header(ir: CardIR) -> Optional[Dict[str, Any]]:
    if not ir.title:
        return None

    # Color based on status
    color_map = {
        "idle": "default",
        "thinking": "blue",
        "working": "orange",
        "done": "green",
        "error": "red",
        "waiting": "gray",
    }
    color = color_map.get(ir.status, "default")

    # v0.3: header title is now "状态: {status_detail}" prefixed,
    # so the user sees live status without scrolling.
    # Original user message kept in subtitle via status_detail for short
    # queries; longer queries fall back to a shorter prefix.
    detail = ir.status_detail or ir.status
    title_text = f"{detail} · {ir.title}" if len(ir.title) <= 40 else detail

    return {
        "template": color,
        "title": {
            "tag": "plain_text",
            "content": title_text,
        },
    }


def _build_body_elements(ir: CardIR) -> List[Dict[str, Any]]:
    elements: List[Dict[str, Any]] = []

    # v0.3: top "see text below" hint, only shown when there's
    # meaningful answer text. This addresses the duplication problem
    # (card and text both carry the answer) by clarifying the relationship.
    # Bug fix: was `getattr(ir, '_answer_text', ir.answer_text)` which is
    # nonsense — _answer_text lives on CardPipeline, not on CardIR.
    if ir.answer_text and ir.status == "done":
        elements.append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": "📌 详细答案在下方文本消息中 · 卡片是进度概览",
            }],
        })

    # Status indicator — v0.3: always visible (was: hidden for idle/done/error)
    status_elem = _render_status(ir)
    if status_elem:
        elements.append(status_elem)

    # Tool progress sections
    if ir.tool_states:
        elements.extend(_render_tools(ir))

    # Answer preview
    if ir.answer_text:
        elements.append({
            "tag": "markdown",
            "content": _truncate_markdown(ir.answer_text, 500),
        })

    # Interaction buttons
    if ir.interaction_buttons:
        elements.append({
            "tag": "action",
            "actions": _render_buttons(ir.interaction_buttons),
        })

    return elements


def _render_status(ir: CardIR) -> Optional[Dict[str, Any]]:
    # v0.3: always return a status row (was: skip for idle/done/error)
    icon_map = {
        "idle": "💬",
        "thinking": "⏳",
        "working": "🔧",
        "waiting": "👆",
        "done": "✅",
        "error": "❌",
    }
    icon = icon_map.get(ir.status, "💬")
    label = ir.status_detail or ir.status

    return {
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": f"{icon} {label}",
            },
        ],
    }


def _render_tools(ir: CardIR) -> List[Dict[str, Any]]:
    elements = []
    for tool_name, state in ir.tool_states.items():
        status = state.get("status", "running")
        result = state.get("result", "")
        duration = state.get("duration_ms", 0)

        # Progress bar
        progress = state.get("progress")
        if progress is not None:
            filled = int(progress * 10)
            bar = "█" * filled + "░" * (10 - filled)
            elements.append({
                "tag": "markdown",
                "content": f"**{tool_name}**\n`{bar}` {int(progress * 100)}%",
            })
        else:
            status_icon = {"running": "🔄", "success": "✅", "error": "❌", "blocked": "🚫"}.get(status, "⏳")
            parts = [f"**{tool_name}** {status_icon}"]
            if result:
                parts.append(f"`{result[:100]}`")
            if duration:
                parts.append(f"({duration}ms)")
            elements.append({
                "tag": "markdown",
                "content": "\n".join(parts),
            })

    return elements


def _render_buttons(buttons: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    actions = []
    for btn in buttons:
        key = btn.get("key", btn.get("value", ""))
        label = btn.get("label", btn.get("text", "确定"))
        variant = btn.get("variant", "primary")

        # Per Feishu Card 2.0 spec, a button that should callback to the bot
        # needs `behaviors` with a callback entry. Without it, the button is
        # inert — the user can tap it but Feishu won't push an event.
        # See: https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/button
        actions.append({
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": label,
            },
            "type": variant,
            "value": {"action": key},   # surfaces as action.value.action in callback
            "behaviors": [
                {
                    "type": "callback",
                    "value": {"action": key},
                }
            ],
        })
    return actions


def _build_footer(ir: CardIR) -> Optional[Dict[str, Any]]:
    """Build footer with state history timeline + last update timestamp.

    v0.3: footer is now always shown (was: only when done). It carries:
      1. The last update timestamp (HH:MM:SS)
      2. A short timeline of recent state transitions (up to 5)
      3. The card edit count
    """
    elements: List[Dict[str, Any]] = []

    # 1. State history timeline
    if ir.state_history:
        timeline_parts = []
        for entry in ir.state_history[-5:]:
            ts = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
            status = entry["status"]
            detail = entry.get("status_detail", "")
            # compact format: "⏳ 思考中... (21:34:25)"
            short = detail if len(detail) <= 20 else detail[:18] + "…"
            timeline_parts.append(f"{ts} {status} {short}")
        if timeline_parts:
            elements.append({
                "tag": "markdown",
                "content": "**状态时间线**\n" + "\n".join(f"- {p}" for p in timeline_parts),
            })

    # 2. Last update + edit count
    ts = time.strftime("%H:%M:%S", time.localtime(ir.updated_at))
    meta = f"更新于 {ts}"
    if ir.edit_count > 0:
        meta += f" · 已更新 {ir.edit_count} 次"
    elements.append({
        "tag": "plain_text",
        "content": meta,
        "style": "small",
    })

    return {"elements": elements}


def _truncate_markdown(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n\n*(内容过长，点击展开)*"


def render_card_update(ir: CardIR, message_key: str) -> Dict[str, Any]:
    """Render a card update for editMessageText API call.

    Returns the card JSON payload.
    """
    return _build_card_body(ir)
