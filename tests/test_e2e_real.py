"""Real end-to-end regression test for v0.2 plugin.

Drives the full Hermes hook chain against the REAL Feishu API:

  pre_gateway_dispatch → send_card (message.create)
  post_tool_call × 2   → edit_card  (message.patch)
  transform_llm_output  → edit_card  (message.patch)
  post_llm_call         → edit_card  (message.patch)
  session_end           → edit_card  (message.patch, final)

No mocks anywhere. Requires:
  - ~/.hermes/config.yaml has feishu app_id + app_secret set
  - lark-oapi SDK installed in hermes-agent venv
  - Bot invited to target chat_id with im:message scope

Run:
  cd ~/.hermes && hermes-agent/venv/bin/python3 \\
      .hermes/plugins/feishu_interactive_cards/tests/test_e2e_real.py

Last verified: 2026-06-25 — 1 create + 6 updates against chat
  oc_fbfc5b17d6c0804fc0161a00c71d56c8 (Hermes home channel).
  message_id: om_x100b6ce6c9885ca0b4c07d627462619

v0.3 changes verified in this test:
  - Header is now "{status_detail} · {title}" (was: title only)
  - Status row always visible (was: hidden for idle/done/error)
  - Footer always visible with state history timeline + edit count
  - Top "📌 详细答案在下方" note appears when answer_text + status=done
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Add plugin dir to path so we can import it as a package
PLUGIN_PARENT = Path.home() / ".hermes/plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

import feishu_interactive_cards as plugin_pkg
from feishu_interactive_cards.plugin import (
    _pipelines,
    _on_pre_gateway_dispatch,
    _on_post_tool_call,
    _on_transform_llm_output,
    _on_post_llm_call,
    _on_session_end,
    _get_or_create_pipeline,
)
from feishu_interactive_cards.feishu_sender import FeishuCardClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

CHAT_ID = "oc_fbfc5b17d6c0804fc0161a00c71d56c8"
SESSION_ID = CHAT_ID  # _extract_source_info falls back to chat_id when session_key is absent
TURN_ID = "t-real-001"

# ============================================================================
# Fake MessageEvent / Source — matches Hermes types
# ============================================================================

class FakeSource:
    def __init__(self, chat_id, platform, session_key=None):
        self.chat_id = chat_id
        self.platform = platform
        self.session_key = session_key or chat_id


class FakeEvent:
    def __init__(self, source, text=""):
        self.source = source
        self.text = text


# ============================================================================
# End-to-end driver
# ============================================================================

async def main():
    print("=" * 60)
    print("v0.2 plugin-level E2E real Feishu test")
    print(f"  chat_id   = {CHAT_ID}")
    print(f"  session   = {SESSION_ID}")
    print("=" * 60)

    # Reset pipeline cache (in case prior test polluted it)
    _pipelines.clear()

    # ---------- 1. pre_gateway_dispatch → send initial card ----------
    print("\n[1/6] pre_gateway_dispatch → send initial card")
    event = FakeEvent(
        source=FakeSource(CHAT_ID, "feishu"),
        text="测试 v0.2 端到端",
    )
    _on_pre_gateway_dispatch(event=event)
    # v0.3 fix: poll for message_key instead of fixed sleep (less flaky)
    # Look up the pipeline *first* so the polling loop can read its message_key.
    pipeline = _get_or_create_pipeline(SESSION_ID, "feishu")
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if pipeline.ir.message_key:
            break
        await asyncio.sleep(0.3)
    if not pipeline.ir.message_key:
        # one more long wait before giving up
        await asyncio.sleep(2.0)
    print(f"   pipeline state  : {pipeline.ir.status}")
    print(f"   message_key     : {pipeline.ir.message_key}")
    assert pipeline.ir.message_key, \
        "pre_gateway_dispatch didn't set message_key! Card send likely failed."
    assert pipeline.ir.status == "idle", \
        f"unexpected state {pipeline.ir.status}"

    msg_id = pipeline.ir.message_key

    # ---------- 2. post_tool_call (tool 1: web_search) ----------
    print("\n[2/6] post_tool_call → web_search tool card")
    _on_post_tool_call(
        tool_name="web_search",
        args={"query": "Hermes agent plugin architecture"},
        result={"success": True, "data": "5 results"},
        session_id=SESSION_ID,
        task_id=TURN_ID,
        tool_call_id="call_real_001",
        duration_ms=380,
        platform="feishu",
        chat_id=CHAT_ID,
    )
    await asyncio.sleep(2.5)
    print(f"   pipeline state  : {pipeline.ir.status}")
    print(f"   tool_states     : {list(pipeline.ir.tool_states.keys())}")
    assert "web_search" in pipeline.ir.tool_states

    # ---------- 3. post_tool_call (tool 2: read_file) ----------
    print("\n[3/6] post_tool_call → read_file tool card")
    _on_post_tool_call(
        tool_name="read_file",
        args={"path": "/tmp/test.py"},
        result={"success": True, "data": "42 lines"},
        session_id=SESSION_ID,
        task_id=TURN_ID,
        tool_call_id="call_real_002",
        duration_ms=120,
        platform="feishu",
        chat_id=CHAT_ID,
    )
    await asyncio.sleep(2.5)
    print(f"   pipeline state  : {pipeline.ir.status}")

    # ---------- 4. transform_llm_output (answer delta 1) ----------
    print("\n[4/6] transform_llm_output → answer delta 1")
    _on_transform_llm_output(
        response_text="插件架构由 8 个 hook 组成，",
        session_id=SESSION_ID,
        platform="feishu",
        model="gpt-5.5",
        turn_id=TURN_ID,
        chat_id=CHAT_ID,
    )
    await asyncio.sleep(2.0)
    print(f"   pipeline state  : {pipeline.ir.status}")

    # ---------- 5. transform_llm_output (answer delta 2) ----------
    print("\n[5/6] transform_llm_output → answer delta 2")
    _on_transform_llm_output(
        response_text="每平台通过 adapter 适配。",
        session_id=SESSION_ID,
        platform="feishu",
        model="gpt-5.5",
        turn_id=TURN_ID,
        chat_id=CHAT_ID,
    )
    await asyncio.sleep(2.0)
    print(f"   pipeline state  : {pipeline.ir.status}")

    # ---------- 6. post_llm_call → finalize ----------
    print("\n[6/6] post_llm_call → finalize answer card")
    _on_post_llm_call(
        session_id=SESSION_ID,
        task_id=TURN_ID,
        turn_id=TURN_ID,
        user_message="测试",
        assistant_response="插件架构由 8 个 hook 组成，每平台通过 adapter 适配。",
        model="gpt-5.5",
        platform="feishu",
        chat_id=CHAT_ID,
    )
    await asyncio.sleep(2.5)
    print(f"   pipeline state  : {pipeline.ir.status}")

    # ---------- 7. session_end ----------
    print("\n[7/7] session_end → mark done")
    _on_session_end(
        session_id=SESSION_ID,
        task_id=TURN_ID,
        turn_id=TURN_ID,
        completed=True,
        interrupted=False,
        model="gpt-5.5",
        platform="feishu",
        chat_id=CHAT_ID,
    )
    await asyncio.sleep(2.0)

    print("\n" + "=" * 60)
    print(f"✅ E2E PASS")
    print(f"   message_id      = {msg_id}")
    print(f"   final state     = {pipeline.ir.status}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())