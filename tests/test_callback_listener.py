"""Test the callback listener's handler logic without a real WebSocket.

v0.3 #4 — verifies:
1. _build_card_action_handler returns a callable that handles Feishu events
2. It routes events to on_button_click with the right payload shape
3. It returns P2CardActionTriggerResponse with success toast
4. start_listener() spawns thread, is_running() reports correctly
5. stop_listener() cleanly joins
6. Double-start is a no-op

The WebSocket client itself is replaced with a no-op (we don't connect
to Feishu's WSS endpoint in unit tests).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PLUGIN_PARENT = Path.home() / ".hermes/plugins"
sys.path.insert(0, str(PLUGIN_PARENT))


def make_feishu_event():
    """Build a P2CardActionTrigger matching what Feishu actually sends.

    Field names verified against lark-oapi SDK 1.5+:
    - P2CardActionTrigger has only `event` field (P2CardActionTriggerData)
    - P2CardActionTriggerData has operator/token/action/host/delivery_type/context
    """
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )
    return P2CardActionTrigger({
        "event": {
            "token": "test_verification_token",
            "action": {
                "value": {"action": "approve", "label": "✅ Approve"},
                "tag": "button",
            },
            "context": {
                "url": "https://example.com/card",
                "open_chat_id": "oc_test_chat",
                "open_message_id": "om_test_msg",
                "preview_token": "test_preview_token",
            },
            "host": "im_message_card",
            "delivery_type": "webhook",
        }
    })


def test_handler_closure():
    """The cleanest test: build a real event, call the closure directly."""
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )
    from feishu_interactive_cards import callback_listener as cl

    received = []

    def on_click(payload):
        received.append(payload)

    handler = cl._build_card_action_handler(on_click)
    event = make_feishu_event()
    result = handler(event)

    # Verify response — SDK uses lark.JSON.marshal() for serialization
    assert isinstance(result, P2CardActionTriggerResponse), \
        f"got {type(result)}"
    resp_dict = json.loads(lark.JSON.marshal(result))
    assert resp_dict.get("toast", {}).get("type") == "success", \
        f"toast: {resp_dict}"
    assert "已收到" in resp_dict["toast"]["content"], \
        f"toast should acknowledge click: {resp_dict['toast']}"
    print(f"   toast: {resp_dict['toast']}")

    # Verify callback
    assert len(received) == 1, f"expected 1 callback, got {len(received)}"
    p = received[0]
    assert p["action"]["value"]["action"] == "approve"
    assert p["context"]["open_chat_id"] == "oc_test_chat"
    assert p["context"]["open_message_id"] == "om_test_msg"
    print(f"   callback payload: {json.dumps(p, ensure_ascii=False)[:200]}")


def test_handler_with_no_on_click():
    """Handler must work even if no callback is wired (graceful no-op)."""
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )
    from feishu_interactive_cards import callback_listener as cl

    handler = cl._build_card_action_handler(on_button_click=None)
    result = handler(make_feishu_event())

    assert isinstance(result, P2CardActionTriggerResponse)
    resp_dict = json.loads(lark.JSON.marshal(result))
    assert resp_dict["toast"]["type"] == "success"
    print(f"   no-callback toast: {resp_dict['toast']}")


def test_handler_returns_error_toast_on_bad_input():
    """If the event is malformed, handler should still return a toast
    (this is the WS contract — the SDK expects a response object)."""
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )
    from feishu_interactive_cards import callback_listener as cl

    handler = cl._build_card_action_handler(on_button_click=None)
    # An empty object — getattr fallback will kick in
    result = handler(object())
    assert isinstance(result, P2CardActionTriggerResponse)
    resp_dict = json.loads(lark.JSON.marshal(result))
    print(f"   bad-input toast: {resp_dict.get('toast', resp_dict)}")


def test_listener_lifecycle():
    """start/stop/is_running behave correctly without real WebSocket."""
    import lark_oapi.ws
    from feishu_interactive_cards import callback_listener as cl

    original_start = lark_oapi.ws.Client.start

    def fake_start(self):
        cl._stop_flag.wait(timeout=3.0)
    lark_oapi.ws.Client.start = fake_start

    try:
        assert not cl.is_running(), "should not be running at start"

        on_click_calls = []
        started = cl.start_listener(
            "cli_test", "secret",
            on_button_click=lambda p: on_click_calls.append(p),
        )
        assert started, "start_listener should return True"
        time.sleep(0.3)
        assert cl.is_running(), "should be running after start"

        stopped = cl.stop_listener(timeout=2.0)
        assert stopped, "stop_listener should return True"
        time.sleep(0.2)
        assert not cl.is_running(), "should not be running after stop"

        # Restart works
        started2 = cl.start_listener("cli_test", "secret")
        assert started2, "restart should work"
        time.sleep(0.3)
        assert cl.is_running()
        cl.stop_listener(timeout=2.0)

        print("   lifecycle: start → run → stop → restart → stop ✓")

    finally:
        lark_oapi.ws.Client.start = original_start
        cl.stop_listener(timeout=1.0)


def test_double_start_skipped():
    """A second start_listener() call while running should be a no-op."""
    import lark_oapi.ws
    from feishu_interactive_cards import callback_listener as cl

    original_start = lark_oapi.ws.Client.start

    def fake_start(self):
        cl._stop_flag.wait(timeout=3.0)
    lark_oapi.ws.Client.start = fake_start

    try:
        assert cl.start_listener("id", "secret")
        time.sleep(0.3)
        result = cl.start_listener("id", "secret")
        assert result is False, f"expected False, got {result}"
        cl.stop_listener(timeout=2.0)
        print("   double-start: second call returns False ✓")
    finally:
        lark_oapi.ws.Client.start = original_start
        cl.stop_listener(timeout=1.0)


if __name__ == "__main__":
    print("=" * 60)
    print("v0.3 #4 — callback listener tests")
    print("=" * 60)

    print("\n1. handler closure with real Feishu event:")
    test_handler_closure()

    print("\n2. handler with no callback (no-op):")
    test_handler_with_no_on_click()

    print("\n3. handler with bad input (graceful error toast):")
    test_handler_returns_error_toast_on_bad_input()

    print("\n4. listener thread lifecycle:")
    test_listener_lifecycle()

    print("\n5. double-start protection:")
    test_double_start_skipped()

    print("\n✅ All v0.3 #4 callback tests passed.")
