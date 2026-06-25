# Changelog

All notable changes to `feishu_interactive_cards` (Hermes plugin).

Format: [Keep a Changelog](https://keepachangelog.com/) style. Versions
follow Hermes' plugin versioning — bump minor on new features, patch
on fixes. Dates in `YYYY-MM-DD`.

## [0.4.0] — 2026-06-25

Cross-turn card deduplication. The plugin now reuses the most recent
card in a chat within a 60s TTL instead of sending a new one for every
turn, so a fast-follow-up message in the same Feishu conversation
edits the existing card (with a `🔄 续接上一张卡片` hint at the top)
rather than creating a new card. Real Feishu verification: 1 new card
sent on turn 1 (`om_x100b6ce05cfd4ca0b3bf4995f23690c`), turn 2 edited
the same message_key, `edit_count` went 0→1.

### Added
- `plugin._get_recent_pipeline_for_chat(chat_id, platform)` — looks
  up the most recent pipeline for a chat; returns `None` if it's
  expired (60s TTL), never had a `message_key`, or platform differs.
- `plugin._CARD_TTL_SECONDS = 60` — configurable TTL window.
- `plugin._pipelines_by_chat: Dict[str, CardPipeline]` — secondary
  index keyed by `chat_id` (the primary key is still `session_id:platform`).
- `tests/test_dedup.py` — 5 unit tests covering: first turn creates
  pipeline, within-TTL reuses pipeline + same message_key, past-TTL
  evicts and creates new, multi-chat isolation, adapter renders
  `🔄` hint and consumes the flag.

### Changed
- `plugin._on_pre_gateway_dispatch` — new dedup branch runs before
  the existing SESSION_START path. When a reusable pipeline is found,
  it skips `pipeline.process_event(SESSION_START)` (which would reset
  `message_key`/`edit_count`/`state_history`) and instead mutates
  just `ir.title` + `ir.status` + `ir._dedup_followup`, then calls
  `_edit_card_async`.
- `adapter_feishu._build_body_elements` — when `ir._dedup_followup` is
  set, prepends a `🔄 续接上一张卡片` note; the flag is `del`'d after
  render so subsequent edits of the same card don't keep showing it.
- `plugin._get_recent_pipeline_for_chat` evicts stale pipelines from
  BOTH `_pipelines_by_chat` and `_pipelines` on TTL expiry (otherwise
  the second call to `_get_or_create_pipeline` would resurrect the
  same object via the session-key index).

### Tests
- `pytest tests/test_dedup.py` — 5/5 passing
- `pytest tests/` — 10/10 passing (5 dedup + 5 callback)
- `pytest tests/test_e2e_dedup.py` — real Feishu: turn 1 sends,
  turn 2 edits same message_key, title updated, `edit_count` 0→1.

## [Unreleased] — v0.3.1 (next)

Bumps the patch number for v0.3. No new features yet planned for this
micro-version. Candidates if we end up doing them:

- Per-tool section dividers (currently tools are a single markdown
  block — splitting makes a long tool run less wall-of-text)
- Color tweak: status="done" green is a bit loud. Try turquoise.

## [0.3.0] — 2026-06-25

First release with **visible product differentiation** — the card now
shows state, time, and tool timing at a glance. Sent + edited 1 real
Feishu card during verification (message_id `om_x100b6ce6b80d94b8b245d934136ee37`).

### Added (v0.3 core — done in 0.3.0)
- **Header now shows state + title together** — `done · user message`
  instead of just `user message`. Color flips: blue (thinking/working)
  → green (done) → red (error) → grey (idle). Drives the user's
  perception of "the card is alive".
- **Top hint when answer is complete** — `📌 详细答案在下方文本消息中 ·
  卡片是进度概览` shown only when `status == "done"` and
  `answer_text` is non-empty. Acknowledges the unavoidable duplication
  (Hermes still sends a text reply in parallel with the card).
- **Status row always visible** — was hidden for `idle` / `done` /
  `error`; v0.3 shows `✅ 完成` / `❌ 出错` etc. so the card never
  looks "blank" in its final state.
- **Tool rows now show duration** — `**web_search** ✅ \`result\` (210ms)`.
  Was just `**web_search** ✅` before. Useful for spotting slow tools.
- **State timeline in footer** — last 5 state transitions with
  HH:MM:SS timestamps: `21:38:38 working 生成回复…`. Was a single
  `完成于 21:38:38` line in v0.2.
- **Edit counter in footer** — `更新于 21:38:38 · 已更新 7 次`. Was
  a fixed single-line in v0.2.
- **Card action buttons (v0.3 #4)** — `IR.interaction_buttons`
  renders as Feishu v2 `tag=action` row. **End-to-end works**:
  user clicks → lark WebSocket pushes `P2CardActionTrigger` event
  → `callback_listener.py` handler routes to the matching pipeline
  → IR transitions `waiting → done` with `已选择: <key>` → card
  re-renders with the click recorded. Files added: `callback_listener.py`
  + `tests/test_callback_listener.py` + `tests/test_e2e_real_buttons.py`.
  Verified against Feishu home channel
  `oc_fbfc5b17d6c0804fc0161a00c71d56c8`, button card
  `om_x100b6ce7689178a0b29ed5899c03c84`, e2e regression card
  `om_x100b6ce7684418a0b045a7e705cc42f`.
  After the "点了没用" fix landed, real-click end-to-end fully
  succeeds: user taps Approve/Reject → `card.action.trigger`
  pushed via WebSocket → handler runs → `edit_count` increments
  → user sees `已选择: approve` in the header. Latest verified
  card: `om_x100b6ce73a5898e0b1f791d66ac0280`.

### Fixed (v0.3 — bugs found while writing the v0.3 dump verifier)
- **`CardIR.answer_text` was never written** — `_on_answer_delta`
  and `_on_answer_end` stored the LLM text in the private
  `self._answer_text` field but never copied it to the IR. The
  adapter reads `ir.answer_text` and rendered an empty body element
  for the final answer. v0.3 fixes this in `session.py` by also
  writing `ir.answer_text = self._answer_text` on every delta + end.
  This was a **silent v0.1/v0.2 bug** that the e2e test never caught
  because we only checked the card *was sent*, not *what it looked
  like*. Caught when I dumped the rendered JSON for v0.3 review.
- **`edit_count` was defined but never incremented** — the field
  existed in `CardIR` from v0.1 but `plugin.py:_edit_card_async`
  only set `message_key` and never bumped the counter. Footer would
  have shown "已更新 0 次" forever. Fixed in `plugin.py` by
  incrementing on every successful `send_card` / `edit_card` call.
- State history was appending duplicates** — `CardIR.update()`
  appended a new entry on *every* call. In a single-turn flow with
  N tool deltas in the same second, the timeline filled with
  identical `(timestamp, status)` pairs. v0.3 fix: only append when
  the *last* entry differs in `status` or `status_detail`. Capped
  at 8 entries to keep the footer from getting too tall.
- **Buttons had no callback behavior → "点了没用"** (real Feishu, 2026-06-25) —
  `_render_buttons()` emitted buttons as `{tag: "button", key, text, type}`
  but **omitted the `behaviors` field**. Per Feishu Card 2.0 spec,
  a button must declare `behaviors: [{type: "callback", value: {...}}]`
  to opt into event delivery — without it the user can tap the button
  but Feishu never pushes a `card.action.trigger` event to the bot.
  Symptom: user tapped the button, toast never appeared, card never
  updated. Fixed in `adapter_feishu.py` by adding the `behaviors`
  array; also surfaced the same key in `action.value.action` so the
  listener handler can extract it directly.
- **Listener handler used `pipeline.ir.turn_id` / `pipeline.ir.platform`**
  — those live on `CardPipeline` (the wrapper), not on `CardIR`
  (the dataclass). The handler crashed with `AttributeError` after
  successfully mutating the IR, so the user never saw the updated
  card. Caught when the real button click triggered the bug but
  `_edit_card_async` never ran because of the exception. Fixed in
  `plugin.py:_on_card_button_clicked`.
- **Listener handler read `action.value.button_key`** — that's a
  mock-test convention we invented. Real Feishu Card 2.0 sends
  `action.value.action` (the string we put in `behaviors[0].value`).
  Handler now reads `action.value.action` first, falls back to
  `action.tag`. Mock payload in `test_callback_listener.py`
  updated to match real shape.
- **e2e test flakiness** — the original test used
  `await asyncio.sleep(2.5)` after `_on_pre_gateway_dispatch` to
  give `_schedule_card_send` time to hit the API. This was
  occasionally too short, causing `message_key` to still be empty
  when the test asserted. Replaced with a polling loop
  (8s deadline, 0.3s poll) for deterministic waiting.
- **`IR.interaction_buttons` was never written by
  `pre_approval_request`** — v0.2 wired the event but never
  populated the IR slot. Buttons defined in `adapter_feishu._render_buttons`
  never showed. v0.3 fix: `_on_pre_approval_request` now writes
  `pipeline.ir.interaction_buttons = buttons` before forwarding
  the event.
- **`_on_interaction_completed` didn't transition out of `waiting`** —
  after a button click, status stayed `waiting` (gray) and the
  card looked "stuck". v0.3 fix: transition to `done`, write
  `status_detail = "已选择: <key>"`, and clear `interaction_buttons`
  so the next render doesn't show stale buttons alongside the
  "done" header.

### Changed
- **Footer is always shown** — was conditional on `status == "done"`.
  Mid-flight cards now also show the timeline, which gives the user
  real-time feedback on what stage they're at.
- **e2e test file** moved to `tests/test_e2e_real.py` (was at
  `/tmp/test_plugin_real_e2e.py`). Not a code change but a
  repository hygiene improvement — tests live in the plugin dir
  so the regression baseline is portable.

### Verification
- ✅ Unit + integration tests pass (`test_e2e_real.py`)
- ✅ Real Feishu send + 6 edits in chat
  `oc_fbfc5b17d6c0804fc0161a00c71d56c8`
- ✅ Rendered JSON dump inspected manually (v0.3 header/timeline/answer
  all present in the JSON sent to Feishu)
- ✅ `tests/test_e2e_real.py` passes twice in a row (flakiness fixed)

## [0.2.0] — 2026-06-25

End-to-end card send + edit pipeline working. Real Feishu verification
on chat `oc_fbfc5b17d6c0804fc0161a00c71d56c8` (Hermes home channel).

### Added
- `plugin.py` — 8 hook handlers, real Feishu card send/edit
- `feishu_sender.py` — `lark-oapi` SDK wrapper. Direct calls, no
  dependency on `gateway/platforms/feishu.py` (which only handles
  `text` and `post` message types).
- `session.py` — `CardPipeline` state machine: `IDLE → THINKING →
  WORKING → ANSWER → SESSION_END`
- `adapter_feishu.py` — Feishu 2.0 card schema. **Flat** structure
  (`header` + `elements` + `footer` at top level) — the nesting
  `body.elements` is for v1 cards, not v2.
- `feishu_sender.py:edit_card` — uses `client.im.v1.message.patch()`
  (NOT `message.update`, which only supports `text` / `post`).
  `PatchMessageRequest` with `token_types={TENANT, USER}` lets
  the SDK handle token fetch automatically. **Going through
  `Transport.execute` directly fails with 400 invalid access token** —
  always use the typed SDK method.

### Fixed during v0.2 (carry-overs from v0.1)
- `register()` must be in `__init__.py`, not `plugin.py`. Hermes'
  PluginManager uses `importlib` to load the *package* (which is
  `__init__.py`), not a sub-module.
- Larks SDK `domain` field is the **full URL** `https://open.feishu.cn`,
  not the string `"feishu"` (SDK treats the latter as a URL scheme
  and dies).
- `lark-oapi` interactive card `content` is a **JSON string**, not
  a dict. `Message.builder()...build()` handles the serialization.
- `client.im.v1.message.update` doesn't support `interactive`
  content. Use `client.im.v1.message.patch()` with `PatchMessageRequest`.

## [0.1.0] — 2026-06-24

Initial scaffold. Architecture choices, no real Feishu integration.
**Status: throwaway** — superseded by v0.2 within hours. Kept in
changelog for archaeology.

### What was tried
- Used `stream_delta` hook — but it's not in `VALID_HOOKS` (Hermes
  doesn't expose it yet). Removed in v0.2.
- Nested card schema `body.elements` — wrong for Feishu 2.0. Fixed
  in v0.2.
- `register()` in `plugin.py` — wrong. PluginManager loads
  `__init__.py`. Fixed in v0.2.

### Files created
- `__init__.py`, `plugin.py`, `events.py`, `session.py`, `render.py`,
  `adapter_feishu.py`, `feishu_sender.py`, `plugin.yaml`, `README.md`

### Total: 8 files, ~1100 lines.
