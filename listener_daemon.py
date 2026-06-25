"""Standalone listener daemon — runs the WS card-click listener
without an associated send-card script. Useful when:

- You sent a card in a previous test run and the listener stopped.
- You want to leave a listener running so future card sends are
  immediately responsive to button clicks.

Usage:
    /Users/ourgang/.hermes/hermes-agent/venv/bin/python3 \\
        ~/.hermes/plugins/feishu_interactive_cards/listener_daemon.py

The listener processes incoming button clicks using the on_button_click
callback registered by plugin.py (loaded via import). To stop it,
Ctrl-C / SIGTERM.

Without --register, this script just starts the listener with the
default `plugin._on_card_button_clicked` callback.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # so `feishu_interactive_cards` resolves

from feishu_interactive_cards.callback_listener import start_listener, is_running
from feishu_interactive_cards.feishu_sender import get_feishu_client


def main():
    client = get_feishu_client()
    if client is None:
        print("❌ get_feishu_client() returned None — credentials missing?")
        sys.exit(1)

    # Load the plugin's actual on_button_click so we exercise the real
    # code path (IR update + edit_card back to Feishu).
    from feishu_interactive_cards import plugin as plugin_module

    started = start_listener(
        app_id=client.app_id,
        app_secret=client.app_secret,
        on_button_click=plugin_module._on_card_button_clicked,
    )
    if not started:
        print("❌ Failed to start listener")
        sys.exit(1)

    print(f"🎧 Listener daemon running. app_id={client.app_id[:10]}...")
    print(f"   Press Ctrl-C to stop.\n")

    stop_flag = {"stop": False}

    def _stop(*_):
        stop_flag["stop"] = True
        print("\nStopping…")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while not stop_flag["stop"]:
            time.sleep(1)
    finally:
        from feishu_interactive_cards.callback_listener import stop_listener
        stop_listener()
        print("✅ Listener stopped.")


if __name__ == "__main__":
    main()
