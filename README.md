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

## Status: v0.3 (Visible Feedback + Clickable Buttons)

✅ Core state machine working
✅ Event schema defined
✅ Card IR renderable
✅ Feishu 2.0 adapter producing valid cards (flat schema: `header` + `elements` + `footer` at top level)
✅ Plugin discovery & hook registration verified
✅ Card sent on `pre_gateway_dispatch` via `lark-oapi` `message.create`
✅ Card edited on subsequent events via `lark-oapi` `message.patch` (NOT `message.update` — that endpoint rejects `interactive` msg_type with `code 230001`)
✅ **Real end-to-end test passing** (see Verification below)
✅ **Header now reflects state in real time** (`done · title`, color flips on transition)
✅ **Status row + state timeline + edit counter in footer**
✅ **Answer text actually renders** (v0.2 silently dropped it — fixed)
✅ **Card action buttons work** — user can click Approve/Reject, the plugin receives the click via lark WebSocket, IR transitions to `done`, card re-renders with click recorded. v0.3 #4.

⏳ **Not yet implemented** (see `ROADMAP.md`):
- **Streaming delta updates** — no `stream_delta` hook in Hermes yet.
  Cards update at `transform_llm_output` time (after LLM finishes),
  not per-token. This is a Hermes upstream change, not a plugin one.
- **Card → final-answer deduplication** — Hermes still sends the
  final answer as a separate text message. Until `post_llm_call`
  supports response replacement, the user sees both.
- **Error recovery** — failed `message.patch` is logged and dropped.
- **Multi-platform** — only the Feishu adapter exists. The IR is
  platform-agnostic, so adapters for Telegram/Discord/Slack are
  pure add-ons, but they don't exist yet.
- **HMAC signature verification on button callbacks** — the v0.3 #4
  listener receives events via lark-oapi's built-in dispatcher (which
  does verification when `encrypt_key`/`verification_token` are
  configured). Production deployments should set those in
  `config.yaml`; v0.3 leaves them empty (no signature = anyone can
  inject clicks, but only clicks matching a real pipeline have any
  effect).

## Usage

Enable in `~/.hermes/config.yaml`:
```yaml
plugins:
  enabled:
    - feishu-interactive-cards
```

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
