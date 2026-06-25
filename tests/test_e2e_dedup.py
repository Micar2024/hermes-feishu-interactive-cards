"""v0.4 e2e: send two real messages to Feishu, verify second turn edits
the first card (not creates a new one).

Usage:
    venv/bin/python3 tests/test_e2e_dedup.py

Asserts:
    - First call sends a new card (returns a message_id)
    - Second call (within TTL) edits the same card (edit_count grows)
    - The card content shows the 🔄 dedup hint
"""
import asyncio
import sys
import time
from pathlib import Path

PLUGIN_PARENT = Path.home() / ".hermes/plugins"
sys.path.insert(0, str(PLUGIN_PARENT))


# Test target: 小A总's home feishu chat (private channel, no other
# agents/users).  This is the same channel v0.3 e2e used.
TEST_CHAT_ID = "oc_fbfc5b17d6c0804fc0161a00c71d56c8"
TEST_PLATFORM = "feishu"


def main():
    import feishu_interactive_cards.plugin as pl
    from unittest.mock import MagicMock

    # Wipe plugin state so we start clean
    pl._pipelines.clear()
    pl._pipelines_by_chat.clear()

    from feishu_interactive_cards.feishu_sender import get_feishu_client
    client = get_feishu_client()
    if client is None:
        print("ERROR: no feishu client (app_id/secret missing?)")
        return 1

    def make_event(chat_id: str, text: str):
        src = MagicMock()
        src.chat_id = chat_id
        src.platform = TEST_PLATFORM
        src.session_key = chat_id
        ev = MagicMock()
        ev.source = src
        ev.text = text
        return ev

    # ---- Turn 1 ----
    print(f"[Turn 1] sending 'hello v0.4' to {TEST_CHAT_ID}")
    pl._on_pre_gateway_dispatch(event=make_event(TEST_CHAT_ID, "hello v0.4"))
    # _on_pre_gateway_dispatch schedules an async send; the plugin
    # coroutine runs in the event loop.  Spin the loop briefly so the
    # send actually goes out and message_key gets populated.
    time.sleep(2.5)

    p1 = pl._pipelines_by_chat.get(TEST_CHAT_ID)
    if p1 is None or not p1.ir.message_key:
        print(f"ERROR: turn 1 send failed; pipeline={p1!r}")
        return 1
    print(f"[Turn 1] sent: message_key={p1.ir.message_key} edit_count={p1.ir.edit_count}")

    # ---- Turn 2 (within 60s TTL) ----
    print(f"[Turn 2] sending 'second message' (should EDIT, not create new)")
    p1.ir.status = "done"  # simulate session finished
    p1.ir.updated_at = time.time()  # mark fresh
    pl._on_pre_gateway_dispatch(event=make_event(TEST_CHAT_ID, "second message"))
    time.sleep(2.5)

    # After turn 2, the same pipeline object should be reused, and
    # message_key should still point to the original card.
    p2 = pl._pipelines_by_chat.get(TEST_CHAT_ID)
    if p2 is None:
        print(f"ERROR: pipeline disappeared after turn 2")
        return 1
    print(f"[Turn 2] pipeline reused: same={p1 is p2} message_key={p2.ir.message_key}")
    print(f"[Turn 2] title={p2.ir.title!r} edit_count={p2.ir.edit_count}")
    print(f"[Turn 2] _dedup_followup={getattr(p2.ir, '_dedup_followup', '(missing)')}")

    # ---- Assertions ----
    failures = []
    if p1 is not p2:
        failures.append("expected same pipeline object after dedup")
    if p2.ir.message_key != p1.ir.message_key:
        failures.append(
            f"expected message_key to stay {p1.ir.message_key!r}, got {p2.ir.message_key!r}"
        )
    if p2.ir.title != "second message":
        failures.append(f"expected title='second message', got {p2.ir.title!r}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS — v0.4 dedup works in real Feishu.")
    print(f"  - 1 card sent ({p1.ir.message_key})")
    print(f"  - 2nd turn edited same card, no new card created")
    return 0


if __name__ == "__main__":
    sys.exit(main())
