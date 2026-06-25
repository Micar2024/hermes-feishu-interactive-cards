# Roadmap — feishu_interactive_cards

This document tracks what each version **did** and what needs to happen
before the next one. **v0.3 is done (shipped 2026-06-25).** v0.4 is
blocked on Hermes upstream `stream_delta` hook — see
**Upstream Dependencies** below.

## Status of v0.3

**Done. Shipped 2026-06-25.**

What v0.3 actually achieved:
- Header carries live state (`done · user message`, color flips)
- Footer carries a 5-entry timeline + edit counter + updated-at
- Tool rows carry their `(duration_ms)`
- Top hint shows when answer is rendered into the card
- **Cards are now interactive**: Approve/Reject buttons trigger
  WebSocket events, IR transitions to `done`, card re-renders
  with the click recorded. v0.3 #4.

Real Feishu verification cards:
- `om_x100b6ce6b80d94b8b245d934136ee37` — v0.3 header/timeline
- `om_x100b6ce7689178a0b29ed5899c03c84` — v0.3 #4 button card
- `om_x100b6ce7684418a0b045a7e705cc42f` — e2e regression

What's still missing for "the user actually sees the value":
- ❌ Still no per-token streaming (cards only update post-LLM)
- ❌ Still no answer deduplication (user sees text reply + card)
- ❌ Still no production opt-out flag (L1)
- ❌ Still no `message.patch` failure recovery (L4)
- ❌ Still single-platform (L5)

## v0.4 — "Stream into the card + dedup"

**This version is blocked on upstream.** See **Upstream Dependencies**
below.

If the `stream_delta` hook lands in Hermes main, v0.4 is a
plugin-only change: subscribe to `stream_delta` events, accumulate
text, throttle patches (e.g. max 4 patches/sec to respect Feishu's
~5 QPS write limit), and `message.patch` the card.

If `post_llm_call` learns to return a replacement string (Upstream #2),
v0.4 also dedups the final answer: instead of Hermes sending "the final
text" as a separate message, the plugin returns the text from
`post_llm_call` and the gateway swallows the original — the card
becomes the only visible artifact.

If neither lands by Q3 2026, **abandon v0.4 in favor of**:
- A "polling" fallback: hook `transform_llm_output` to read the
  most recent `_stream_consumer._text_buffer` (private API, will
  break across versions)
- OR a Feishu-side hack: open a websocket to the gateway and read
  `_fire_stream_delta`'s callback chain directly (will require a
  separate gateway process permission)

**Both fallbacks are bad. The right answer is the upstream hook.**

## v0.5 — "Multi-platform adapters + production polish"

Once v0.3 + v0.4 are solid on Feishu, the IR/render code is platform-
agnostic. v0.5 is mostly boilerplate:

- `adapter_telegram.py` — Telegram InlineKeyboard buttons +
  `editMessageText` (already supports in-place edit; better than
  Feishu for streaming)
- `adapter_discord.py` — Discord components v2 + `edit_message` (5
  edits / 5 sec limit; need aggressive throttling)
- `adapter_slack.py` — Slack Block Kit + `chat.update` (3 edits/sec;
  need an edit queue)
- `adapter_wechat.py` — WeChat 客服消息 (limited interactivity; the
  customer-service API doesn't support in-place edits at all — WeChat
  adapter can only send fresh messages, no live cards)

Plus production polish (carried from v0.3):
- L1: per-message opt-out flag (`feishu_interactive_cards.enabled`)
- L4: `message.patch` failure → fresh `message.create` with snapshot
- HMAC signature verification on button callbacks (production
  security; v0.3 shipped without it — fine for personal use)

## Known Limitations (v0.3 — current state)

These are **documented, not fixed**. They will surface as user-visible
issues if you enable the plugin in production.

### L1. Plugin auto-activates in production ⚠️

`~/.hermes/config.yaml:631` lists `feishu-interactive-cards` under
`plugins:`, which means **every Feishu message** is intercepted from
the moment the gateway starts. There is no per-message opt-in. If you
want to disable without removing the plugin files, comment that line
out.

**Fix planned for v0.3**: read `FEISHU_INTERACTIVE_CARDS_ENABLED` env
var or a `feishu_interactive_cards.enabled` config flag.

### L2. Streaming not supported (documented elsewhere)

`stream_delta` is not in `hermes_cli.plugins.VALID_HOOKS` (the 20
hooks Hermes exposes to plugins). Streaming chunks go through
`run_agent._fire_stream_delta` and `GatewayStreamConsumer.on_delta`,
both of which are **core** APIs not reachable from a plugin.

**Fix**: see Upstream Dependencies #1.

### L3. Button callbacks without HMAC signature verification

v0.3 #4 wired up Feishu card action buttons, but the lark-oapi
listener is started **without** an `encrypt_key`/`verification_token`
pair. That means any client that can reach Feishu (which is the
internet) can synthesize a `P2CardActionTrigger` event and inject
clicks into our pipelines.

In practice this is low-risk:
- The injected click has to match a real `open_chat_id` +
  `open_message_id` for the pipeline to even acknowledge it.
- Clicking a button only mutates IR state — it doesn't execute
  code or send anything outside Feishu.
- The blast radius is "user sees a card that's been clicked by
  someone other than themselves".

If we ever deploy this plugin somewhere where multi-tenant or
untrusted users share the same bot, configure
`feishu_interactive_cards.lark_encrypt_key` and
`...lark_verification_token` in `config.yaml`. v0.5 work item.

### L4. Silent failure on `message.patch` errors

If a `message.patch` call fails (network blip, rate limit, message
expired), the plugin logs the error and **continues**. The state
machine keeps transitioning, so subsequent patches target a
non-existent message_id. The user sees a card that "stops updating"
with no indication why.

**Fix**: catch patch failures, send a new `message.create` with the
full current IR snapshot, swap the pipeline's `message_id` to the
new one, retry.

### L5. `asyncio.create_task` fire-and-forget in hook functions

`_schedule_card_send` in `plugin.py` does
`asyncio.get_running_loop().create_task(coro)`. If the event loop
closes (gateway shutdown, session reset) before the task completes,
the SDK call is dropped mid-flight and we leak an unfinished Future.

**Risk**: low (the task is small and Hermes event loops are long-
lived), but it's a latent bug. Fix: track the task in the
`CardPipeline` and `await` it on `on_session_end`.

## Upstream Dependencies

These are PRs/requests **we would file against Hermes main** that
unblock v0.4+:

### #1. `stream_delta` plugin hook (CRITICAL for v0.4)

**What**: add `"stream_delta"` to
`hermes_cli.plugins.VALID_HOOKS` and emit it from
`run_agent._fire_stream_delta` (or from a new
`GatewayStreamConsumer._fire_delta` call site for the gateway path).

**Why it's allowed**: AGENTS.md says
> "A hook is NOT speculative if a contributor has a real, stated use
> case — even if the consumer ships separately."

We have a real use case. The hook is one-liner in core. **Easy PR.**

**Effort estimate**: ~30 lines core + 50 lines test. Should be a
first-week contribution.

### #2. `post_llm_call` should be able to **return** a string (IMPORTANT for v0.3)

**What**: `post_llm_call` currently returns `None` (observe only).
Make it return a string that **replaces** the LLM response if
non-None, mirroring `transform_llm_output`.

**Why it matters for us**: we want to swallow the final text answer
in v0.3 and render it into the card. Today we can only observe, not
replace. Without this, the user still sees "final text answer" + a
card — defeating the point of the card.

**Why it's allowed**: `transform_llm_output` already has this
contract. `post_llm_call` is asymmetric by historical accident, not
by design.

**Effort estimate**: ~20 lines core. **Easy PR.**

### #3. `pre_gateway_dispatch` should expose the gateway's `WebhookServer` reference (NICE for v0.3+)

**What**: pass the running webhook server into the hook kwargs so
plugins can register additional routes (e.g. for card action
callbacks).

**Why it matters**: we don't want to spin up a SECOND HTTP server
just to handle button clicks — we want to share the existing
gateway's webhook listener.

**Why it's not required**: we can run a separate listener on a
different port. Just messier.

**Effort estimate**: ~40 lines core + a new `HookContext` field.
**Medium PR.**

## What we are NOT planning

- **iOS / Android / desktop native cards**: the platform IR is for
  messaging adapters only. Native app cards are out of scope.
- **Voice / video call cards**: Feishu has these in a different API
  family (`vc/v1`). Different schema, different auth. Punt to a
  separate plugin.
- **Persistent cards across gateway restarts**: the CardPipeline
  state is in-memory. If the gateway restarts mid-session, the card
  is orphaned and the next session starts a new card. Fixing this
  needs a state-store (Redis, SQLite, etc.) — overkill for v0.3-0.5.

## Open questions for the user

1. **Push to GitHub?** If yes, what repo? `feishu_interactive_cards`
   under your personal account, or a `hermes-contrib` org?
2. **License?** Plugin source is all original. Suggest MIT, matching
   Hermes itself. Confirm before adding `LICENSE`.
3. **Brand name?** Currently `feishu_interactive_cards`. Rename to
   `interactive_cards` once a second adapter lands, or keep platform-
   specific names?
