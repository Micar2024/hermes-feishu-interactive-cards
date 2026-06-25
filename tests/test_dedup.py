"""v0.4: cross-turn card deduplication.

Verifies:
1. First turn in a chat → pipeline is created, no existing card, falls
   through to "send new card" path.
2. Second turn in the same chat within TTL → reuses existing pipeline
   + message_key, sets _dedup_followup flag, returns early (no new card).
3. Second turn after TTL elapsed → existing pipeline is forgotten, new
   pipeline is created, send new card path runs.
4. Different chat_id → never reuses another chat's pipeline.
5. Adapter renders the 🔄 续接 hint when _dedup_followup is set, and
   clears the flag after rendering (so it shows only once per dedup).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

PLUGIN_PARENT = Path.home() / ".hermes/plugins"  # parent of the package
sys.path.insert(0, str(PLUGIN_PARENT))


def _plugin():
    """Import the plugin module under its package name so relative
    imports inside plugin.py resolve to feishu_interactive_cards.*"""
    import feishu_interactive_cards.plugin as pl
    return pl


def _make_event(chat_id: str, text: str = "hi", platform: str = "feishu"):
    """Build a minimal MessageEvent-like object."""
    src = MagicMock()
    src.chat_id = chat_id
    src.platform = platform
    src.session_key = chat_id
    ev = MagicMock()
    ev.source = src
    ev.text = text
    return ev


def _reset_module_state():
    """Wipe plugin module-level dicts so tests don't leak state."""
    import importlib
    pl = _plugin()
    importlib.reload(pl)
    pl._pipelines.clear()
    pl._pipelines_by_chat.clear()
    return pl


def test_first_turn_creates_pipeline_and_calls_send(monkeypatch):
    pl = _reset_module_state()
    sent_to: list = []
    monkeypatch.setattr(pl, "_schedule_card_send",
                        lambda coro: sent_to.append(coro) or None)
    # Stub out the feishu client to keep the test offline
    monkeypatch.setattr("feishu_sender.get_feishu_client", lambda: None)

    ev = _make_event(chat_id="oc_chat_1", text="first turn")
    pl._on_pre_gateway_dispatch(event=ev)

    # pipeline created for the chat
    assert "oc_chat_1" in pl._pipelines_by_chat
    p = pl._pipelines_by_chat["oc_chat_1"]
    assert p.ir.title == "first turn"
    # send path was scheduled (asyncio.Task via _schedule_card_send)
    assert len(sent_to) == 1


def test_second_turn_within_ttl_reuses_pipeline(monkeypatch):
    pl = _reset_module_state()
    sent_to: list = []
    edited_to: list = []
    monkeypatch.setattr(pl, "_schedule_card_send",
                        lambda coro: sent_to.append(coro) or None)
    monkeypatch.setattr(pl, "_edit_card_async",
                        lambda pipeline, **kw: edited_to.append((pipeline.ir.message_key, kw)) or None)
    monkeypatch.setattr("feishu_sender.get_feishu_client", lambda: None)

    # Turn 1
    pl._on_pre_gateway_dispatch(event=_make_event("oc_chat_2", "turn 1"))
    p1 = pl._pipelines_by_chat["oc_chat_2"]
    p1.ir.message_key = "om_card_alpha"  # simulate successful send
    p1.ir.status = "done"
    p1.ir.updated_at = time.time()  # fresh

    # Turn 2 (same chat, within TTL)
    pl._on_pre_gateway_dispatch(event=_make_event("oc_chat_2", "turn 2"))

    # no NEW card sent
    assert len(sent_to) == 1, f"expected 1 send, got {len(sent_to)}"
    # edit path was called for the same pipeline
    assert len(edited_to) == 1
    msg_key, kwargs = edited_to[0]
    assert msg_key == "om_card_alpha", f"expected to edit om_card_alpha, got {msg_key}"
    # same pipeline object (reused)
    assert pl._pipelines_by_chat["oc_chat_2"] is p1
    # dedup flag was set by the plugin (consumption is the adapter's job —
    # we test that in test_adapter_renders_dedup_hint_and_clears_flag)
    assert p1.ir._dedup_followup is True
    # simulate the adapter consumption, then check the second turn path
    from feishu_interactive_cards.adapter_feishu import render_feishu_card
    render_feishu_card(p1.ir)
    assert p1.ir._dedup_followup is False


def test_second_turn_after_ttl_creates_new_pipeline(monkeypatch):
    pl = _reset_module_state()
    sent_to: list = []
    monkeypatch.setattr(pl, "_schedule_card_send",
                        lambda coro: sent_to.append(coro) or None)
    monkeypatch.setattr("feishu_sender.get_feishu_client", lambda: None)

    pl._on_pre_gateway_dispatch(event=_make_event("oc_chat_3", "turn 1"))
    p1 = pl._pipelines_by_chat["oc_chat_3"]
    p1.ir.message_key = "om_old"
    p1.ir.status = "done"
    # backdate past TTL
    p1.ir.updated_at = time.time() - (pl._CARD_TTL_SECONDS + 5)

    pl._on_pre_gateway_dispatch(event=_make_event("oc_chat_3", "turn 2"))
    p2 = pl._pipelines_by_chat["oc_chat_3"]

    assert p2 is not p1, "expected new pipeline after TTL"
    assert p2.ir.message_key == ""  # new card, no msg_key yet
    # send was scheduled twice (once per turn)
    assert len(sent_to) == 2


def test_different_chat_never_reuses():
    pl = _reset_module_state()
    pl._pipelines_by_chat["oc_A"] = MagicMock(platform="feishu", ir=MagicMock(message_key="om_a"))
    pl._pipelines_by_chat["oc_B"] = MagicMock(platform="feishu", ir=MagicMock(message_key="om_b"))

    assert pl._get_recent_pipeline_for_chat("oc_A", "feishu").ir.message_key == "om_a"
    assert pl._get_recent_pipeline_for_chat("oc_C", "feishu") is None


def test_adapter_renders_dedup_hint_and_clears_flag():
    """Adapter must render the 🔄 hint when _dedup_followup is set, and
    clear the flag so it doesn't show on subsequent renders of the same IR."""
    from feishu_interactive_cards.adapter_feishu import render_feishu_card
    from feishu_interactive_cards.session import CardPipeline

    p = CardPipeline("sess_dedup", platform="feishu")
    setattr(p.ir, "_dedup_followup", True)
    p.ir.title = "dedup test"
    p.ir.answer_text = ""  # skip the "📌 详细答案" hint to isolate the dedup one
    p.ir.status = "thinking"

    card1 = render_feishu_card(p.ir)
    elems1 = card1["elements"]
    # find the 🔄 note
    dedup_notes = [e for e in elems1
                   if e.get("tag") == "note"
                   and any("续接上一张卡片" in (m.get("content", ""))
                           for m in e.get("elements", []))]
    assert len(dedup_notes) == 1, f"expected 1 dedup hint, got {dedup_notes}"
    # flag must be cleared after render
    assert getattr(p.ir, "_dedup_followup", None) is False

    # re-render — hint should NOT appear again
    card2 = render_feishu_card(p.ir)
    elems2 = card2["elements"]
    dedup_notes2 = [e for e in elems2
                    if e.get("tag") == "note"
                    and any("续接上一张卡片" in (m.get("content", ""))
                            for m in e.get("elements", []))]
    assert len(dedup_notes2) == 0, f"hint leaked into second render: {dedup_notes2}"