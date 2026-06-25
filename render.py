"""Platform-agnostic JSON-IR renderer.

Converts CardIR to a platform-independent intermediate representation.
Used for debugging and as a base for platform adapters.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .session import CardIR, CardSection


def render_ir(ir: CardIR) -> Dict[str, Any]:
    """Render CardIR to a flat JSON-IR dict."""
    return {
        "meta": {
            "status": ir.status,
            "status_detail": ir.status_detail,
            "created_at": ir.created_at,
            "updated_at": ir.updated_at,
            "edit_count": ir.edit_count,
            "message_key": ir.message_key,
        },
        "title": ir.title,
        "sections": [_section_to_dict(s) for s in ir.sections],
        "tools": list(ir.tool_states.values()),
        "answer": ir.answer_text,
        "buttons": ir.interaction_buttons,
    }


def _section_to_dict(section: CardSection) -> Dict[str, Any]:
    return {
        "tag": section.tag,
        "variant": section.variant,
        "content": section.content,
        "fields": section.fields,
        "actions": section.actions,
        "metadata": section.metadata,
    }
