"""Event schema — mirrors Hermes plugin hook payloads.

Defines the canonical event types for the interactive card pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EventType(str, Enum):
    """Canonical event types."""
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    THINKING_START = "thinking.start"
    THINKING_DELTA = "thinking.delta"
    THINKING_END = "thinking.end"
    TOOL_START = "tool.start"
    TOOL_DELTA = "tool.delta"
    TOOL_END = "tool.end"
    ANSWER_DELTA = "answer.delta"
    ANSWER_END = "answer.end"
    INTERACTION_REQUESTED = "interaction.requested"
    INTERACTION_COMPLETED = "interaction.completed"
    INTERACTION_FAILED = "interaction.failed"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SessionStart:
    session_id: str
    platform: str
    model: str
    user_message: Optional[str] = None


@dataclass
class SessionEnd:
    session_id: str
    completed: bool
    interrupted: bool
    model: str


@dataclass
class ThinkingStart:
    session_id: str
    turn_id: str
    model: str


@dataclass
class ThinkingDelta:
    session_id: str
    turn_id: str
    content: str
    accumulated_length: int = 0


@dataclass
class ThinkingEnd:
    session_id: str
    turn_id: str
    total_tokens: int = 0


@dataclass
class ToolStart:
    session_id: str
    turn_id: str
    tool_name: str
    tool_args: Dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""


@dataclass
class ToolDelta:
    session_id: str
    turn_id: str
    tool_name: str
    tool_call_id: str
    status: str = "running"  # running / partial / complete / error
    content: str = ""
    progress: Optional[float] = None  # 0.0 - 1.0


@dataclass
class ToolEnd:
    session_id: str
    turn_id: str
    tool_name: str
    tool_call_id: str
    result_summary: str = ""
    status: str = "success"  # success / error / blocked
    duration_ms: int = 0


@dataclass
class AnswerDelta:
    session_id: str
    turn_id: str
    content: str
    is_final: bool = False


@dataclass
class AnswerEnd:
    session_id: str
    turn_id: str
    total_chars: int = 0


@dataclass
class InteractionRequested:
    session_id: str
    turn_id: str
    message_type: str  # approval / choice / input
    buttons: List[Dict[str, Any]] = field(default_factory=list)
    callback_data: Optional[Dict[str, Any]] = None


@dataclass
class InteractionCompleted:
    session_id: str
    turn_id: str
    button_key: str
    button_value: Any


@dataclass
class InteractionFailed:
    session_id: str
    turn_id: str
    error_type: str
    error_message: str


@dataclass
class Error:
    session_id: str
    turn_id: str
    error_type: str
    error_message: str


# ---------------------------------------------------------------------------
# Factory helpers — build event from hook kwargs
# ---------------------------------------------------------------------------

def build_event(event_type: EventType, **kwargs: Any) -> Any:
    """Build a typed event object from kwargs."""
    cls = {
        EventType.SESSION_START: SessionStart,
        EventType.SESSION_END: SessionEnd,
        EventType.THINKING_START: ThinkingStart,
        EventType.THINKING_DELTA: ThinkingDelta,
        EventType.THINKING_END: ThinkingEnd,
        EventType.TOOL_START: ToolStart,
        EventType.TOOL_DELTA: ToolDelta,
        EventType.TOOL_END: ToolEnd,
        EventType.ANSWER_DELTA: AnswerDelta,
        EventType.ANSWER_END: AnswerEnd,
        EventType.INTERACTION_REQUESTED: InteractionRequested,
        EventType.INTERACTION_COMPLETED: InteractionCompleted,
        EventType.INTERACTION_FAILED: InteractionFailed,
        EventType.ERROR: Error,
    }.get(event_type)
    if cls is None:
        raise ValueError(f"Unknown event type: {event_type}")
    return cls(**kwargs)


def event_to_dict(event: Any) -> Dict[str, Any]:
    """Serialize event to dict for JSON-IR rendering."""
    if hasattr(event, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(event)
    if isinstance(event, dict):
        return event
    return {"type": str(type(event).__name__), "data": str(event)}
