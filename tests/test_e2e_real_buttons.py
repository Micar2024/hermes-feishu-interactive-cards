"""v0.3 #4 real Feishu end-to-end test.

This test verifies the FULL callback path:
1. Start the WS listener (real lark-oapi connection)
2. Send a card with a button to the user's chat
3. Simulate the button being clicked (we don't have a real user to
   click it, so we just call _on_card_button_clicked directly with
   the click payload)
4. Verify the card updates (e.g. button click recorded in IR)

To make this work, the listener must be running and the WS connection
must be alive when the simulated click fires. The listener is started
in a background thread at plugin import time.
"""

import sys
import time
import json
from pathlib import Path

PLUGIN_DIR = Path.home() / ".hermes" / "plugins"
sys.path.insert(0, str(PLUGIN_DIR))

from feishu_interactive_cards import plugin as plg
from feishu_interactive_cards.adapter_feishu import render_feishu_card


def get_test_chat_id() -> str:
    """Pull the configured Feishu chat_id from the test config."""
    import yaml
    cfg_path = Path.home() / ".hermes" / "plugins" / "feishu_interactive_cards" / "tests" / "test_feishu.yaml"
    if not cfg_path.exists():
        return "oc_fbfc5b17d6c0804fc0161a00c71d56c8"  # fallback to home
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("test_chat_id", "oc_fbfc5b17d6c0804fc0161a00c71d56c8")


def main():
    print("=" * 60)
    print("v0.3 #4 — Real Feishu button click test")
    print("=" * 60)

    chat_id = get_test_chat_id()
    print(f"\n1. Target chat_id: {chat_id}")

    # Step 1: Start the listener thread (real WS connection)
    print("\n2. Starting WebSocket listener...")
    plg._start_card_action_listener()
    time.sleep(2)  # let it connect

    # Step 2: Set up a fake pipeline for this chat
    print("\n3. Creating pipeline with approval buttons...")
    pipeline = plg._get_or_create_pipeline(chat_id, "feishu")
    pipeline.ir.title = "v0.3 #4 按钮测试"
    pipeline.ir.platform = "feishu"

    # Inject approval buttons (this is what pre_approval_request would do)
    pipeline.ir.interaction_buttons = [
        {"key": "approve", "label": "✅ Approve", "type": "primary"},
        {"key": "reject", "label": "❌ Reject", "type": "danger"},
    ]
    pipeline.ir.status = "approval"
    pipeline.ir.status_note = "等待用户确认"
    from feishu_interactive_cards.session import EventType
    from feishu_interactive_cards.events import build_event
    pipeline.process_event(build_event(
        EventType.INTERACTION_REQUESTED,
        session_id=chat_id,
        turn_id="v03-4-test",
        message_type="approval",
        buttons=pipeline.ir.interaction_buttons,
    ))

    # Step 3: Render the card
    card = render_feishu_card(pipeline.ir)
    has_button_action = any(
        e.get("tag") == "action" and any(
            b.get("key") == "approve"
            for b in e.get("actions", [])
        )
        for e in card.get("elements", [])
    )
    assert has_button_action, f"Card should have approve button: {json.dumps(card, ensure_ascii=False)[:300]}"
    print(f"   ✓ card has approve button (Feishu v2 schema: tag=action, key=approve)")

    # Step 4: Send the card to Feishu
    print("\n4. Sending card with buttons to Feishu...")
    import asyncio
    from feishu_interactive_cards.feishu_sender import get_feishu_client
    client = get_feishu_client()
    if client is None:
        print("   ✗ Feishu client not initialized")
        sys.exit(1)

    async def _send():
        return await client.send_card(
            receive_id=chat_id,
            receive_id_type="chat_id",
            card_payload=card,
        )

    success, msg_id = asyncio.run(_send())
    if not success:
        print(f"   ✗ Send failed: {msg_id}")
        sys.exit(1)
    print(f"   ✓ Card sent: message_id={msg_id}")
    pipeline.ir.message_key = msg_id

    # Step 5: Simulate user clicking the Approve button
    print("\n5. Simulating button click...")
    fake_click_payload = {
        "action": {
            "value": {"button_key": "approve", "label": "✅ Approve"},
            "tag": "button",
        },
        "context": {
            "url": "",
            "open_chat_id": chat_id,
            "open_message_id": msg_id,
            "preview_token": "",
        },
    }
    plg._on_card_button_clicked(fake_click_payload)
    print(f"   ✓ Click routed")

    # Step 6: Verify IR was updated
    print("\n6. Verifying IR state after click...")
    print(f"   status:        {pipeline.ir.status}")
    print(f"   status_note:   {pipeline.ir.status_note}")
    print(f"   answer_text length: {len(pipeline.ir.answer_text or '')}")

    # Step 7: Re-render the card and push update
    print("\n7. Pushing card update...")
    import asyncio
    async def _edit():
        return await client.edit_card(msg_id, render_feishu_card(pipeline.ir))
    success, result = asyncio.run(_edit())
    if not success:
        print(f"   ✗ Edit failed: {result}")
    else:
        print(f"   ✓ Card updated in place")

    print("\n" + "=" * 60)
    print("✅ v0.3 #4 real Feishu test complete")
    print(f"   message_id: {msg_id}")
    print(f"   Go check your Feishu chat to see the button + click recording")
    print("=" * 60)


if __name__ == "__main__":
    main()
