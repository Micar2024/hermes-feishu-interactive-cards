"""ConversationState machine for the interactive card pipeline.

Tracks the lifecycle of a single Hermes turn:
  IDLE → SESSION_START → THINKING → TOOL → ANSWER → SESSION_END

Transitions are driven by incoming events. Each state maintains
a mutable JSON-IR buffer that gets progressively updated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .events import (
    EventType,
    AnswerDelta,
    AnswerEnd,
    Error,
    InteractionCompleted,
    InteractionFailed,
    InteractionRequested,
    SessionEnd,
    SessionStart,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingStart,
    ToolDelta,
    ToolEnd,
    ToolStart,
    event_to_dict,
)


# ---------------------------------------------------------------------------
# IR (Intermediate Representation) — platform-agnostic card structure
# ---------------------------------------------------------------------------

@dataclass
class CardSection:
    """A single section in the card IR."""
    tag: str          # header / body / footer / status
    variant: str      # primary / warning / danger / neutral
    content: str = ""
    fields: List[Dict[str, str]] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CardIR:
    """Platform-agnostic card representation."""
    title: str = ""
    sections: List[CardSection] = field(default_factory=list)
    status: str = "idle"  # idle / thinking / working / done / error
    status_detail: str = ""
    tool_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    answer_text: str = ""
    interaction_buttons: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    message_key: str = ""  # filled after first send
    edit_count: int = 0
    # v0.3: state transition history (timestamp, status, status_detail)
    # capped at last 5 entries to keep footer small
    state_history: List[Dict[str, Any]] = field(default_factory=list)

    def update(self):
        self.updated_at = time.time()
        # v0.3: record every status change for footer timeline
        # Bug fix: only append if the *last* entry differs from current state
        # (was: appending on every .update() call, creating duplicates when
        # the same status was set multiple times in a single second)
        last = self.state_history[-1] if self.state_history else None
        if last is None or last["status"] != self.status \
                or last.get("status_detail") != self.status_detail:
            self.state_history.append({
                "ts": self.updated_at,
                "status": self.status,
                "status_detail": self.status_detail,
            })
            # cap to last 5 to keep card compact
            if len(self.state_history) > 5:
                self.state_history = self.state_history[-5:]

    def add_section(self, section: CardSection):
        self.sections.append(section)
        self.update()

    def replace_section(self, tag: str, section: CardSection):
        for i, s in enumerate(self.sections):
            if s.tag == tag:
                self.sections[i] = section
                break
        else:
            self.add_section(section)
        self.update()


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class CardPipeline:
    """Manages IR state for a single conversation turn."""

    def __init__(self, session_id: str, platform: str = ""):
        self.session_id = session_id
        self.platform = platform
        self.turn_id: Optional[str] = None
        self.ir = CardIR(title="", status="idle")
        self._lock = False  # simplified: no threading lock for v0.1
        self._tool_order: List[str] = []
        self._tool_status: Dict[str, str] = {}
        self._thinking_text: str = ""
        self._answer_text: str = ""

    def process_event(self, event: Any) -> Optional[CardIR]:
        """Process an event, mutate IR, return updated IR if changed."""
        import dataclasses

        # Handle both dataclass events and dict events
        if dataclasses.is_dataclass(event):
            etype = event.__class__.__name__.lower().replace("start", "_start") \
                         .replace("end", "_end") \
                         .replace("delta", "_delta") \
                         .replace("requested", "_requested") \
                         .replace("completed", "_completed") \
                         .replace("failed", "_failed")
            # Map class name -> event type value
            class_to_etype = {
                "SessionStart": EventType.SESSION_START.value,
                "SessionEnd": EventType.SESSION_END.value,
                "ThinkingStart": EventType.THINKING_START.value,
                "ThinkingDelta": EventType.THINKING_DELTA.value,
                "ThinkingEnd": EventType.THINKING_END.value,
                "ToolStart": EventType.TOOL_START.value,
                "ToolDelta": EventType.TOOL_DELTA.value,
                "ToolEnd": EventType.TOOL_END.value,
                "AnswerDelta": EventType.ANSWER_DELTA.value,
                "AnswerEnd": EventType.ANSWER_END.value,
                "InteractionRequested": EventType.INTERACTION_REQUESTED.value,
                "InteractionCompleted": EventType.INTERACTION_COMPLETED.value,
                "InteractionFailed": EventType.INTERACTION_FAILED.value,
                "Error": EventType.ERROR.value,
            }
            etype = class_to_etype.get(event.__class__.__name__, etype)
            # Convert to dict for handlers
            event_dict = dataclasses.asdict(event)
            event_dict["_event_type"] = etype
            event = event_dict
        elif not isinstance(event, dict):
            event = event_to_dict(event)

        etype = event.get("type", event.get("_event_type", ""))

        handlers = {
            EventType.SESSION_START.value: self._on_session_start,
            EventType.SESSION_END.value: self._on_session_end,
            EventType.THINKING_START.value: self._on_thinking_start,
            EventType.THINKING_DELTA.value: self._on_thinking_delta,
            EventType.THINKING_END.value: self._on_thinking_end,
            EventType.TOOL_START.value: self._on_tool_start,
            EventType.TOOL_DELTA.value: self._on_tool_delta,
            EventType.TOOL_END.value: self._on_tool_end,
            EventType.ANSWER_DELTA.value: self._on_answer_delta,
            EventType.ANSWER_END.value: self._on_answer_end,
            EventType.INTERACTION_REQUESTED.value: self._on_interaction_requested,
            EventType.INTERACTION_COMPLETED.value: self._on_interaction_completed,
            EventType.INTERACTION_FAILED.value: self._on_interaction_failed,
            EventType.ERROR.value: self._on_error,
        }

        handler = handlers.get(etype)
        if handler:
            return handler(event)
        return None

    # -- Session lifecycle --------------------------------------------------

    def _on_session_start(self, event: Dict[str, Any]) -> Optional[CardIR]:
        self.turn_id = event.get("turn_id", "")
        self.ir.status = "idle"
        self.ir.title = event.get("user_message", "")[:80] or "新对话"
        self.ir.answer_text = ""
        self.ir.tool_states.clear()
        self.ir.interaction_buttons.clear()
        self.ir.edit_count = 0
        self.ir.message_key = ""
        self._tool_order.clear()
        self._tool_status.clear()
        self._thinking_text = ""
        self._answer_text = ""
        self.ir.update()
        return self.ir

    def _on_session_end(self, event: Dict[str, Any]) -> Optional[CardIR]:
        completed = event.get("completed", True)
        interrupted = event.get("interrupted", False)
        if interrupted:
            self.ir.status = "error"
            self.ir.status_detail = "用户中断"
        elif not completed:
            self.ir.status = "error"
            self.ir.status_detail = "执行失败"
        else:
            self.ir.status = "done"
            self.ir.status_detail = "完成"
        self.ir.update()
        return self.ir

    # -- Thinking -----------------------------------------------------------

    def _on_thinking_start(self, event: Dict[str, Any]) -> Optional[CardIR]:
        self.ir.status = "thinking"
        self.ir.status_detail = "思考中..."
        self._thinking_text = ""
        self.ir.update()
        return self.ir

    def _on_thinking_delta(self, event: Dict[str, Any]) -> Optional[CardIR]:
        chunk = event.get("content", "")
        self._thinking_text += chunk
        self.ir.status_detail = f"思考中... ({len(self._thinking_text)} 字)"
        self.ir.update()
        return self.ir

    def _on_thinking_end(self, event: Dict[str, Any]) -> Optional[CardIR]:
        self.ir.status = "working"
        self.ir.status_detail = "工作中..."
        self.ir.update()
        return self.ir

    # -- Tool ---------------------------------------------------------------

    def _on_tool_start(self, event: Dict[str, Any]) -> Optional[CardIR]:
        tool_name = event.get("tool_name", "unknown")
        if tool_name not in self._tool_order:
            self._tool_order.append(tool_name)
        self._tool_status[tool_name] = "running"
        self.ir.tool_states[tool_name] = {
            "status": "running",
            "name": tool_name,
            "started_at": time.time(),
        }
        if self.ir.status != "working":
            self.ir.status = "working"
            self.ir.status_detail = f"执行 {tool_name}..."
        self.ir.update()
        return self.ir

    def _on_tool_delta(self, event: Dict[str, Any]) -> Optional[CardIR]:
        tool_name = event.get("tool_name", "")
        status = event.get("status", "running")
        content = event.get("content", "")
        progress = event.get("progress")

        ts = self.ir.tool_states.get(tool_name, {})
        ts["status"] = status
        ts["content"] = content
        if progress is not None:
            ts["progress"] = progress
        ts["updated_at"] = time.time()
        self.ir.tool_states[tool_name] = ts
        self.ir.status_detail = f"执行 {tool_name} ({status})"
        self.ir.update()
        return self.ir

    def _on_tool_end(self, event: Dict[str, Any]) -> Optional[CardIR]:
        tool_name = event.get("tool_name", "")
        result = event.get("result_summary", "")
        status = event.get("status", "success")
        duration = event.get("duration_ms", 0)

        ts = self.ir.tool_states.get(tool_name, {})
        ts["status"] = status
        ts["result"] = result
        ts["duration_ms"] = duration
        ts["completed_at"] = time.time()
        self.ir.tool_states[tool_name] = ts
        self.ir.status_detail = f"工具完成: {tool_name}"
        self.ir.update()
        return self.ir

    # -- Answer -------------------------------------------------------------

    def _on_answer_delta(self, event: Dict[str, Any]) -> Optional[CardIR]:
        chunk = event.get("content", "")
        self._answer_text += chunk
        # v0.3 fix: also write to ir.answer_text so the adapter can render it
        # (was: only stored on self._answer_text — never visible in card body)
        self.ir.answer_text = self._answer_text
        self.ir.status_detail = "生成回复..."
        self.ir.update()
        return self.ir

    def _on_answer_end(self, event: Dict[str, Any]) -> Optional[CardIR]:
        # v0.3 fix: also write to ir.answer_text for the same reason
        self.ir.answer_text = self._answer_text
        self.ir.status_detail = "回复完成"
        self.ir.update()
        return self.ir

    # -- Interaction --------------------------------------------------------

    def _on_interaction_requested(self, event: Dict[str, Any]) -> Optional[CardIR]:
        buttons = event.get("buttons", [])
        self.ir.interaction_buttons = buttons
        self.ir.status = "waiting"
        self.ir.status_detail = "等待用户操作"
        self.ir.update()
        return self.ir

    def _on_interaction_completed(self, event: Dict[str, Any]) -> Optional[CardIR]:
        # v0.3 #4: also transition out of "waiting" so the footer /
        # header color reflects "user has responded". Without this the
        # card was stuck on waiting/gray even after the user clicked.
        button_key = event.get("button_key", "")
        self.ir.status = "done"
        self.ir.status_detail = f"已选择: {button_key}" if button_key else "已响应"
        # v0.3: clear the buttons after the click so the next card update
        # doesn't show stale "Approve/Reject" buttons alongside a "done"
        # state.
        self.ir.interaction_buttons = []
        self.ir.update()
        return self.ir

    def _on_interaction_failed(self, event: Dict[str, Any]) -> Optional[CardIR]:
        self.ir.status_detail = f"操作失败: {event.get('error_message', '')}"
        self.ir.update()
        return self.ir

    # -- Error --------------------------------------------------------------

    def _on_error(self, event: Dict[str, Any]) -> Optional[CardIR]:
        self.ir.status = "error"
        self.ir.status_detail = event.get("error_message", "未知错误")
        self.ir.update()
        return self.ir

    def get_ir(self) -> CardIR:
        return self.ir
