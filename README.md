# Feishu Interactive Cards Plugin for Hermes

Platform-agnostic interactive card rendering for Hermes conversations.
Hooks into the official Hermes plugin system — **no monkey-patching**,
**no sidecar**, **no HTTP server** (yet).

> **Status: v0.3 — visible feedback + clickable buttons (working).**
> See [`CHANGELOG.md`](CHANGELOG.md) for what changed in v0.3 and
> [`ROADMAP.md`](ROADMAP.md) for what v0.4+ needs.

## ⚠️ Production activation

This plugin is **enabled by default** if you cloned it into
`~/.hermes/plugins/feishu_interactive_cards/` AND have it in your
`~/.hermes/config.yaml` `plugins:` list. That means **every Feishu
message** gets intercepted.

To disable without removing the files, comment it out of `config.yaml`
(see `ROADMAP.md` L1 for the long-term opt-out flag).

## Credentials

The plugin reads Feishu app credentials from `~/.hermes/config.yaml`
(`feishu.app_id`, `feishu.app_secret`). Set them there, **or** put
`FEISHU_APP_ID` and `FEISHU_APP_SECRET` in `~/.hermes/.env` and the
plugin will pick them up.

**Never commit credentials to git.** This repo is clean — `grep -rE
"app_secret|app_id|client_secret" --exclude-dir=__pycache__` returns
only parameter declarations and config-loading code, no hardcoded
values.

## Architecture

```
Hermes Gateway
  │
  ├─ pre_gateway_dispatch ──→ create initial card (idle)
  ├─ post_tool_call ────────→ update card with tool progress
  ├─ transform_llm_output ──→ update card with response text
  ├─ post_llm_call ─────────→ finalize card
  ├─ pre_approval_request ──→ add interaction buttons
  ├─ post_approval_response → update card on button click
  ├─ on_session_start ──────→ clean slate
  └─ on_session_end ────────→ close card
```

## Components

| Module | Purpose | Lines |
|---|---|---|
| `events.py` | Event schema (dataclasses mirroring Hermes hook payloads) | ~190 |
| `session.py` | `CardPipeline` state machine: IDLE→THINKING→TOOL→ANSWER→DONE | ~290 |
| `render.py` | Platform-agnostic JSON-IR renderer | ~40 |
| `adapter_feishu.py` | Feishu 2.0 interactive card adapter (IR → 飞书 schema) | ~210 |
| `plugin.py` | Hermes plugin entry point (register_hook calls + button click routing) | ~370 |
| `feishu_sender.py` | `lark-oapi` SDK wrapper, sends/edits cards, reads config for app creds | ~210 |
| `callback_listener.py` | lark-oapi WebSocket listener, receives card button click events, routes to pipeline | ~200 |

## How It Works

1. **Event Pipeline**: Each Hermes hook fires an event → `CardPipeline.process_event()` → mutates `CardIR` → returns updated IR
2. **State Machine**: Tracks turn lifecycle (idle → thinking → working → answer → done)
3. **Card Rendering**: `CardIR` → platform-specific card schema (Feishu 2.0 interactive card)
4. **Card Dispatch**: `feishu_sender.py` calls `lark-oapi` SDK directly (no monkey-patch, no gateway modification)

## Status: v0.4.0 (Withdrawal Buttons + Cross-Turn Dedup + Edit-Bug Fix)

✅ v0.1 Core state machine + Feishu 2.0 adapter (flat schema)
✅ v0.2 Card sent on `pre_gateway_dispatch` + edited on subsequent events via `message.patch`
✅ v0.3 Visible feedback (header state, status row, state timeline, edit counter)
✅ v0.3 #4 Clickable buttons — Approve/Reject wired through lark WebSocket
✅ v0.4 Cross-turn deduplication — same chat within 60s TTL edits the existing card (`🔄 续接上一张卡片` hint), instead of creating a new one
✅ v0.4 #3 **Card withdrawal button** — `撤回卡片` (type=danger) on every card, every state. Click → `DELETE /open-apis/im/v1/messages/:id` → pipeline marked `withdrawn` → next turn creates fresh card. Closes the loop on user error.
✅ **Real end-to-end verified** (see Verification):
- `om_x100b6ce0d99c08a8b49affc012b1c82` — card sent + withdrawn (real Feishu delete API, `success=True, result='ok'`)
- `om_x100b6ce05cfd4ca0b3bf4995f23690c` — card sent, turn-2 edited same message_key (dedup path)

⏳ **Not yet implemented** (see `ROADMAP.md`):
- **Streaming delta updates** — no `stream_delta` hook in Hermes yet. Cards update at `transform_llm_output` time, not per-token. Hermes upstream change.
- **Card → final-answer deduplication** — Hermes still sends the final answer as a separate text message. Until `post_llm_call` supports response replacement, the user sees both.
- **HMAC signature verification on button callbacks** — v0.3 #4 listener relies on lark-oapi's built-in dispatcher. Production deployments should set `encrypt_key` / `verification_token` in `config.yaml`.
- **Multi-platform adapters** (v0.5+ deferred) — only Feishu adapter exists. The IR is platform-agnostic; Telegram/Discord/Slack adapters are pure add-ons, but they don't exist yet. See `ROADMAP.md` for why this is deferred until v0.5.

## Usage: How to Test the Withdrawal Button Manually

After enabling the plugin and sending any message to your Feishu bot, the card will arrive with a red `撤回卡片` button at the bottom. To test the full withdrawal flow:

1. Open Feishu, find the card in the chat
2. Click the red `撤回卡片` button
3. Within ~1s, the card should disappear from your chat
4. In the plugin logs (`~/.hermes/logs/hermes.log` or wherever Hermes pipes stdout), you should see:
   ```
   [plugin.feishu-interactive-cards] card withdrawn: message_id=om_xxx
   [plugin.feishu-interactive-cards] delete_card success: (True, "ok")
   ```
5. Send a new message to the bot — a fresh card should appear (the previous pipeline is gone, marked `status="withdrawn"`).

If the card doesn't disappear, check `~/.hermes/config.yaml`:
- `plugins.enabled` contains `feishu-interactive-cards`
- `feishu.app_id` and `feishu.app_secret` are set
- The WebSocket listener is running (lark-oapi dispatches `P2CardActionTrigger` events; if the gateway's listener is dead, clicks won't arrive)

## Future: Add `stream_delta` Hook (Hermes Upstream)

The plugin cannot receive real-time text deltas because Hermes doesn't
expose a `stream_delta` hook. Detailed analysis + estimated effort is
in `ROADMAP.md` under **Upstream Dependencies** #1.

Short version: add `"stream_delta"` to
`hermes_cli.plugins.VALID_HOOKS` and call `invoke_hook("stream_delta",
text=delta)` from `run_agent.py:_fire_stream_delta`. ~30 lines core +
50 lines test. **Easy PR.**

## Comparison: Us vs Them

| Aspect | This Plugin | baileyh8/hermes-feishu-streaming-card |
|---|---|---|
| Patching | ❌ None | ✅ AST patch `run.py` |
| Sidecar | ❌ None | ✅ aiohttp HTTP server |
| Plugin System | ✅ Official hooks | ❌ Monkey-patch |
| Streaming Delta | ⏳ Pending Hermes hook | ✅ Custom emit |
| Card Dispatch | ✅ `lark-oapi` direct call | ✅ aiohttp + local server |
| Multi-Channel | ✅ Designed for it | ❌ Feishu only |

## Verification

### 1. Real Feishu end-to-end (regression baseline)

`tests/test_e2e_real.py` drives the full Hermes hook chain against the real
Feishu API — no mocks, no sidecar, no patch of Hermes core.

```bash
cd ~/.hermes && hermes-agent/venv/bin/python3 \
    .hermes/plugins/feishu_interactive_cards/tests/test_e2e_real.py
```

Last verified 2026-06-25 against chat `oc_fbfc5b17d6c0804fc0161a00c71d56c8`:

```
[1/6] pre_gateway_dispatch  → send_card   (message.create) ✓
[2/6] post_tool_call × 2    → edit_card   (message.patch)  ✓
[4/6] transform_llm_output  → edit_card   (message.patch)  ✓
[5/6] post_llm_call         → edit_card   (message.patch)  ✓
[6/6] session_end           → edit_card   (message.patch, final) ✓

Total SDK calls: 1 create + 6 patch
State transitions: idle → working → working → working → working → working → done
Final message_id: om_x100b6ce6de8c2938b345b78ea318adc
```

### 2. Mock integration (1 create + 4 update per session)

Earlier validation using mocked SDK to verify the state machine + adapter
pipeline without hitting the real API. Lives in git history — superseded by
the real e2e above.

### 3. Sample Card Output (Feishu 2.0 flat schema)

```json
{
  "msg_type": "interactive",
  "content": "{\"header\":{\"template\":\"green\",\"title\":{\"tag\":\"plain_text\",\"content\":\"帮我查一下深圳今天的天气\"}},\"elements\":[{\"tag\":\"markdown\",\"content\":\"**web_search** ✅\\n`深圳晴 28°C`\\n(230ms)\"}],\"footer\":{\"elements\":[{\"tag\":\"plain_text\",\"content\":\"完成于 20:44:25\"}]}}"
}
```

Note: `content` is JSON-stringified (the SDK's `Message.builder().build()` does
this automatically when given a dict via `CreateMessageRequestBody`).

## Why `message.patch` and not `message.update`

`message.update` only supports `text` and `post` msg_types. For `interactive`
cards it returns `code 230001 invalid msg_type`. The dedicated
`PATCH /im/v1/messages/:message_id` endpoint is the only way to edit an
interactive card in-place — it's hidden as `Message.patch()` in lark-oapi's
generated client.

## License

MIT
