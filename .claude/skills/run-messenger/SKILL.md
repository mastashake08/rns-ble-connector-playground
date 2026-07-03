---
name: run-messenger
description: Launch the interactive LXMF messaging client, explain its keybindings (M compose, I inbox, P presence, Q quit), or explain the presence directory / contacts.json. Use when the user wants to send or receive LXMF messages, browse known peers, or start lxmf_messenger.py.
---

# Running the LXMF messenger

```
source .venv/bin/activate
python3 lxmf_messenger.py
```

This brings up Reticulum itself — attaching to `rnsd` as a shared-instance client if it's already running (e.g. from `rnode_pair.py`), or opening the configured interfaces directly if not — reuses the same identity file, then drops into a single-keypress UI (no Enter needed):

- **M** — compose: paste a recipient's LXMF address (hex), optional title, message body
- **I** — inbox: lists received messages, pick a number to reply
- **P** — presence directory: lists every LXMF peer seen announcing on the network (not just people who've messaged you), pick a number to message them directly, or press **A** to re-announce yourself
- **Q** — quit

Incoming messages and new contacts both trigger a terminal bell alert plus a native macOS notification.

## How the presence directory works

`lxmf_messenger.py` registers an `RNS.Transport` announce handler filtered to `lxmf.delivery` — the aspect namespace any LXMF client announces under — so it hears every peer's announce network-wide, decodes their display name from the announce's `app_data`, and persists it to `contacts.json`. This is mutual: someone else won't see you unless you've announced too, which happens once at startup by default. Use `--announce-interval <minutes>` to re-announce periodically instead.

## Flags

`--config`, `--identity` — same meaning/defaults as `rnode_pair.py` (see `add-config-profile` skill for the config picker).
`--display-name` — shown to peers when you announce.
`--stamp-cost` — proof-of-work senders must pay before delivery (anti-spam); default `0`.
`--contacts <path>` — where the presence directory is persisted; default `./contacts.json`.
`--announce-interval <minutes>` — periodic self-announce; default `0` (announce once at startup only).

## Sending to someone you don't have a saved address for

You need their LXMF address (hex destination hash) out of band, or wait for their client to announce and pick them up in the **P** screen. There's no network-wide directory query beyond listening for announces — if they haven't announced since you were last listening, request-a-path will time out (~15s) and the send will report it can't find them.
