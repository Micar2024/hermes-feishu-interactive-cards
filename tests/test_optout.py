"""v0.5 #1: per-message opt-out flag (feishu_interactive_cards.enabled).

Covers:
1. _is_enabled() default-on when config has no feishu_interactive_cards node
2. _is_enabled() default-on when feishu_interactive_cards.enabled is unset
3. _is_enabled() returns False when feishu_interactive_cards.enabled == False
4. _is_enabled() returns True on any read error (fail-open)
5. _on_pre_gateway_dispatch short-circuits when disabled
6. _on_card_button_clicked no-ops when disabled
7. _edit_card_async and _delete_card_async do not call feishu client
8. _start_card_action_listener skips WebSocket startup when disabled

All tests use monkeypatch to flip _is_enabled() at the boundary, instead
of mutating ~/.hermes/config.yaml. The helper function itself is
exercised by feeding it a temp config via monkeypatched Path.home().
"""
import sys
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch as _patch

PLUGIN_PARENT = Path.home() / ".hermes" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

import pytest

import feishu_interactive_cards.plugin as pl


@pytest.fixture
def fresh_plugin():
    """Reset module-level state between tests."""
    pl._pipelines.clear()
    pl._pipelines_by_chat.clear()
    return pl


# --- _is_enabled() helper ---------------------------------------------------


def test_is_enabled_default_true_when_config_missing(fresh_plugin, monkeypatch, tmp_path):
    """No config file at all → enabled (default)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert fresh_plugin._is_enabled() is True


def test_is_enabled_default_true_when_node_absent(fresh_plugin, monkeypatch, tmp_path):
    """Config exists but has no feishu_interactive_cards node → enabled."""
    cfg = tmp_path / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("feishu:\n  app_id: dummy\n  app_secret: dummy\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert fresh_plugin._is_enabled() is True


def test_is_enabled_default_true_when_field_absent(fresh_plugin, monkeypatch, tmp_path):
    """Node exists but no `enabled` key → default True (v0.4 compat)."""
    cfg = tmp_path / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("feishu_interactive_cards:\n  some_other_key: 1\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert fresh_plugin._is_enabled() is True


def test_is_enabled_false_when_explicitly_disabled(fresh_plugin, monkeypatch, tmp_path):
    cfg = tmp_path / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("feishu_interactive_cards:\n  enabled: false\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert fresh_plugin._is_enabled() is False


def test_is_enabled_true_when_explicitly_enabled(fresh_plugin, monkeypatch, tmp_path):
    cfg = tmp_path / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("feishu_interactive_cards:\n  enabled: true\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert fresh_plugin._is_enabled() is True


def test_is_enabled_fails_open_on_bad_yaml(fresh_plugin, monkeypatch, tmp_path):
    """Garbled config file → still default True (don't silently kill the plugin)."""
    cfg = tmp_path / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("this: is: not: valid: yaml: ::\n  - broken\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert fresh_plugin._is_enabled() is True


# --- pre_gateway_dispatch short-circuit ------------------------------------


def test_pre_gateway_dispatch_short_circuits_when_disabled(fresh_plugin, monkeypatch):
    monkeypatch.setattr(pl, "_is_enabled", lambda: False)
    fake_event = MagicMock()
    fake_event.text = "hello"
    # Make it look like a Feishu chat event
    from feishu_interactive_cards.plugin import _extract_source_info
    monkeypatch.setattr(
        pl, "_extract_source_info",
        lambda e: ("oc_test_chat", "feishu", "oc_test_chat"),
    )
    # If we *didn't* short-circuit, this would create a pipeline and
    # call _edit_card_async. Verify it doesn't.
    pl._on_pre_gateway_dispatch(event=fake_event, turn_id="t1", model="m1")
    assert pl._pipelines == {}
    assert pl._pipelines_by_chat == {}


def test_pre_gateway_dispatch_passes_through_when_enabled(fresh_plugin, monkeypatch):
    """Sanity: ensure the guard isn't accidentally always-on."""
    monkeypatch.setattr(pl, "_is_enabled", lambda: True)
    monkeypatch.setattr(
        pl, "_extract_source_info",
        lambda e: ("oc_test_chat", "feishu", "oc_test_chat"),
    )
    # Mock the feishu client to a no-op so we don't actually hit the network
    from feishu_interactive_cards import feishu_sender
    monkeypatch.setattr(feishu_sender, "get_feishu_client", lambda: None)
    fake_event = MagicMock()
    fake_event.text = "hello"
    pl._on_pre_gateway_dispatch(event=fake_event, turn_id="t1", model="m1")
    # When enabled, a pipeline IS created
    assert "oc_test_chat:feishu" in pl._pipelines


# --- _on_card_button_clicked guard -----------------------------------------


def test_button_click_no_op_when_disabled(fresh_plugin, monkeypatch):
    pipeline = pl._get_or_create_pipeline("oc_test_chat", "feishu")
    pipeline.ir.status = "done"
    pipeline.ir.title = "test"
    pipeline.ir.message_key = "om_x"
    pl._index_pipeline_by_chat(pipeline, "oc_test_chat")

    monkeypatch.setattr(pl, "_is_enabled", lambda: False)
    # If the guard is missing, this would transition IR to withdrawn
    # (because the button value below is card_withdraw).
    pl._on_card_button_clicked({
        "context": {"open_chat_id": "oc_test_chat"},
        "action": {"value": {"action": "card_withdraw"}},
    })
    # IR must be untouched
    assert pipeline.ir.status == "done"
    assert pipeline.ir.title == "test"


def test_button_click_routes_normally_when_enabled(fresh_plugin, monkeypatch):
    """Sanity: guard isn't a no-op always."""
    pipeline = pl._get_or_create_pipeline("oc_test_chat", "feishu")
    pipeline.ir.status = "done"
    pipeline.ir.message_key = "om_x"
    pl._index_pipeline_by_chat(pipeline, "oc_test_chat")

    monkeypatch.setattr(pl, "_is_enabled", lambda: True)
    fake_delete = MagicMock()
    monkeypatch.setattr(pl, "_delete_card_async", fake_delete)

    pl._on_card_button_clicked({
        "context": {"open_chat_id": "oc_test_chat"},
        "action": {"value": {"action": "card_withdraw"}},
    })
    assert fake_delete.call_count == 1
    assert pipeline.ir.status == "withdrawn"


# --- _edit_card_async / _delete_card_async guard ---------------------------


def test_edit_card_async_no_op_when_disabled(fresh_plugin, monkeypatch):
    pipeline = pl._get_or_create_pipeline("oc_test_chat", "feishu")
    pipeline.ir.title = "x"
    pipeline.ir.message_key = "om_x"

    monkeypatch.setattr(pl, "_is_enabled", lambda: False)
    # If guard is missing, the function imports adapter_feishu and
    # feishu_sender and calls get_feishu_client. Patch those to fail
    # loudly so a missing guard would error.
    def boom(*a, **kw):
        raise AssertionError("adapter_feishu should not be imported when disabled")
    monkeypatch.setattr(
        "feishu_interactive_cards.adapter_feishu.render_feishu_card", boom
    )

    pl._edit_card_async(pipeline, "oc_test_chat", "feishu")
    # No exception means the guard short-circuited before any work.


def test_delete_card_async_no_op_when_disabled(fresh_plugin, monkeypatch):
    pipeline = pl._get_or_create_pipeline("oc_test_chat", "feishu")
    pipeline.ir.message_key = "om_x"

    monkeypatch.setattr(pl, "_is_enabled", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("feishu_sender should not be touched when disabled")
    monkeypatch.setattr(
        "feishu_interactive_cards.feishu_sender.get_feishu_client", boom
    )

    pl._delete_card_async(pipeline, "oc_test_chat", "feishu")


# --- _start_card_action_listener guard ------------------------------------


def test_start_listener_skips_when_disabled(fresh_plugin, monkeypatch):
    """When disabled, the WebSocket listener should not be started."""
    monkeypatch.setattr(pl, "_is_enabled", lambda: False)

    # Patch the SDK entry point so a missing guard would actually call
    # into it (and presumably fail on missing creds, but at least we'd
    # know the guard didn't fire).
    start_called = {"v": False}

    def fake_start(*a, **kw):
        start_called["v"] = True
        return True

    monkeypatch.setattr(
        "feishu_interactive_cards.callback_listener.start_listener", fake_start
    )
    pl._start_card_action_listener()
    assert start_called["v"] is False, (
        "WebSocket listener was started despite enabled=False"
    )
