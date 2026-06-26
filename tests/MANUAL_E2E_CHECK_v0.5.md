# v0.5 #1 — Manual end-to-end verification checklist

Use this when you want to verify the per-message opt-out flag actually
silences the plugin in real Feishu traffic. Unit tests cover the helper
+ guards, but only a real gateway restart against a real Feishu chat
confirms the network side is truly silent.

## When to run this

- After pulling `4b704c7` (v0.5.0) and wanting to confirm production
  behavior before relying on the flag.
- Whenever you change something around `_is_enabled()` or the guard
  call sites.

## Pre-conditions

- [ ] Current Hermes session ended (or you don't mind losing it — the
      gateway restart will kill it).
- [ ] No important background tasks / cron jobs running.
- [ ] `~/.hermes/config.yaml` accessible.

## Steps

### 1. Set the flag

Edit `~/.hermes/config.yaml`, append at the bottom (or merge into
existing `feishu:` block — but the dedicated node is cleaner):

```yaml
feishu_interactive_cards:
  enabled: false
```

### 2. Restart the gateway

How you do this depends on your launchd / supervisord setup. On
this machine it's typically:

```bash
launchctl kickstart -k gui/$(id -u)/com.hermes.gateway
```

(Substitute your actual launchd label or `kill + relaunch` if you
run it manually.)

### 3. Trigger a real message

Send any message to the Feishu bot from your phone or the desktop
client. A short test like "ping" is fine.

### 4. Verify the three assertions

| Assertion | Where to check | Expected |
|---|---|---|
| No card rendered | Feishu chat on the receiving side | Plain text only, no interactive card UI |
| Listener not started | gateway stdout/stderr or `~/.hermes/logs/` | One line: `[feishu-interactive-cards] Disabled by config; card action listener not started` |
| No SDK call | Same logs | **No** line containing `Initial card sent` or `Edit failed` for this message |

### 5. Verify the inverse (sanity)

Flip the flag to `true` (or remove the `feishu_interactive_cards:`
node entirely) and restart again. Send another message. The card
should reappear as it did in v0.4.0.

## If something is wrong

- Card still renders despite `enabled: false` → check the file
  actually has the new node. The helper is fail-open, so YAML
  indentation errors will silently default to enabled.
  Run `python3 -c "import yaml; print(yaml.safe_load(open('/Users/ourgang/.hermes/config.yaml')).get('feishu_interactive_cards'))"`
  to confirm the node parsed.
- Listener started anyway → check `git log` that you have
  `4b704c7` and not an older commit. The guard is in
  `_start_card_action_listener`; if the file is stale the guard
  is missing.
- Anything else → paste the gateway log + the relevant
  `plugin.py` section, debug from there.

## Cleanup

After verification, decide whether to leave the flag set or revert.
For ongoing production use, `enabled: false` is the long-term state
if you want the plugin loaded but silent. For normal use, remove
the node entirely.
