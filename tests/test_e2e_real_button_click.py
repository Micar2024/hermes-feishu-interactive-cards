"""v0.3 #4 — REAL Feishu button click end-to-end (single-process).

Single-process: builds the pipeline, sends the card, starts the
listener, waits for click, and updates the card — all in one Python
process. This is required because the listener handler looks up
pipelines from a module-level dict, and cross-process pipelines
don't exist.

Run:
    /Users/ourgang/.hermes/hermes-agent/venv/bin/python3 \\
        ~/.hermes/plugins/feishu_interactive_cards/tests/test_e2e_real_button_click.py

You'll need to manually click a button in Feishu within 90s of
running this.

For automated testing without a human, see test_e2e_real_buttons.py
which feeds a synthetic click event directly into the handler closure.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE.parent))  # so `feishu_interactive_cards` resolves

from feishu_interactive_cards.callback_listener import start_listener, stop_listener  # noqa: E402
from feishu_interactive_cards.events import EventType, build_event  # noqa: E402
from feishu_interactive_cards.feishu_sender import get_feishu_client  # noqa: E402
from feishu_interactive_cards.plugin import _pipelines, _on_card_button_clicked  # noqa: E402
from feishu_interactive_cards.session import CardPipeline  # noqa: E402
from feishu_interactive_cards.adapter_feishu import render_feishu_card  # noqa: E402


HOME_CHAT_ID = "oc_fbfc5b17d6c0804fc0161a00c71d56c8"


def main():
    client = get_feishu_client()
    if client is None:
        print("❌ get_feishu_client() returned None — credentials missing?")
        sys.exit(1)

    # 1. Pre-register a pipeline keyed by chat_id (matches plugin handler lookup)
    pipeline_key = f"{HOME_CHAT_ID}:feishu"
    pipeline = CardPipeline(session_id=HOME_CHAT_ID, platform="feishu")
    _pipelines[pipeline_key] = pipeline
    pipeline.ir.title = "请选择一个动作"
    pipeline.ir.interaction_buttons = [
        {"key": "approve", "label": "✅ Approve", "type": "primary"},
        {"key": "reject", "label": "❌ Reject", "type": "danger"},
    ]
    pipeline.ir.status = "waiting"
    pipeline.ir.status_detail = "等待用户选择"
    pipeline.ir.update()

    # 2. Send the card
    print(f"\n=== Sending card to chat {HOME_CHAT_ID} ===\n")

    async def _send():
        return await client.send_card(
            receive_id=HOME_CHAT_ID,
            receive_id_type="chat_id",
            card_payload=render_feishu_card(pipeline.ir),
        )
    ok, msg_or_id = asyncio.run(_send())
    if not ok:
        print(f"❌ send_card failed: {msg_or_id}")
        sys.exit(1)
    pipeline.ir.message_key = msg_or_id
    print(f"✅ Card sent. message_id = {msg_or_id}")

    # 3. Start the listener WITHIN THIS PROCESS so it shares _pipelines
    started = start_listener(
        app_id=client.app_id,
        app_secret=client.app_secret,
        on_button_click=_on_card_button_clicked,
    )
    if not started:
        print("❌ Failed to start listener")
        sys.exit(1)

    print(f"🎧 Listener running. Waiting up to 90s for real click...\n")
    print(f"   Open Feishu → chat {HOME_CHAT_ID}")
    print(f"   Find card with title '等待用户选择 · 请选择一个动作'")
    print(f"   Tap ✅ Approve or ❌ Reject.\n")

    # 4. Poll until pipeline.ir.status != "waiting" (handler will change it)
    deadline = time.time() + 90
    while time.time() < deadline:
        if pipeline.ir.status != "waiting":
            break
        time.sleep(0.5)

    stop_listener()

    # 5. Report
    print("\n" + "=" * 60)
    if pipeline.ir.status == "waiting":
        print("⏰ Timed out — no click received within 90s.")
        print(f"   Card still live at message_id={msg_or_id}.")
        print(f"   You can still click; the next plugin run will see it.")
        sys.exit(0)

    print("🎉 REAL button click received and routed through plugin handler!")
    print()
    print(f"   Final card state:")
    print(f"     status        = {pipeline.ir.status}")
    print(f"     status_detail = {pipeline.ir.status_detail}")
    print(f"     edit_count    = {pipeline.ir.edit_count}")
    print(f"     message_key   = {pipeline.ir.message_key}")
    print()
    print(f"   Open Feishu → the buttons should be gone and the header")
    print(f"   should now read '{pipeline.ir.status_detail} · {pipeline.ir.title}'.")
    print("=" * 60)


if __name__ == "__main__":
    main()
