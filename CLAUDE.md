# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two standalone Python scripts (no package/build system) that get a Reticulum ([RNS](https://reticulum.network/)) node running on this Mac with an [RNode](https://unsigned.io/rnode/) connected over Bluetooth LE, plus an LXMF messaging client on top:

- `rnode_pair.py` — pairs an RNode over BLE and wires it into an RNS config
- `lxmf_messenger.py` — interactive LXMF client; imports `create_or_load_identity` and `resolve_config_dir` from `rnode_pair.py`, so that file is a shared module as well as a script

## Commands

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Inside the venv, use `python3 -m pip install <pkg>` rather than a bare `pip install` — on this machine `pip` is shell-aliased to a system pip3 that bypasses the venv, so `python3 -m pip` is the reliable way to actually install into `.venv`.

Run the two entry points directly:
```
python3 rnode_pair.py        # pair a new RNode, or re-run to just update config + launch rnsd
python3 lxmf_messenger.py    # interactive messaging client (M compose, I inbox, P presence, Q quit)
```

There is no lint config, build step, or automated test suite. Verification is done by:
- `python3 -m py_compile <file>.py` for a syntax check
- ad hoc runs against a scratch config directory (pass `--config`/`--identity`/`--contacts`/`--state-file` pointing at a temp dir instead of the real `~/.reticulum` or repo state) with `unittest.mock.patch` used to script `input()` prompts and `serial.tools.list_ports.comports()` — this is necessary because real behavior involves live BLE/USB hardware and an interactive TTY that can't be part of an automated suite

## Architecture

### Config resolution is shared and dual-mode

Both scripts' `main()` call `resolve_config_dir()` (defined in `rnode_pair.py`) before doing anything else. If `--config` isn't passed explicitly, it interactively prompts to choose between the user's live `~/.reticulum` config and any saved profile directory under `configs/<name>/` (detected by `list_saved_configs()` — any subdirectory of `configs/` containing a `config` file). Picking a profile uses that directory as a fully isolated RNS config dir (own `storage/`, own interface state) rather than touching the live one. Passing `--config` explicitly skips the prompt. This same function is imported into `lxmf_messenger.py` rather than duplicated.

### Why `rnode_pair.py` talks raw KISS over serial

RNode's BLE stack requires OS-level Bluetooth bonding before any data can flow, and macOS only exposes that pairing dialog through System Settings — no library can drive it programmatically. What *can* be automated is talking to the RNode over USB serial using the same KISS commands `rnodeconf` uses (`CMD_BT_CTRL` to enable Bluetooth / enter pairing mode, `CMD_BT_PIN` to read back the generated pairing PIN), then walking the user through completing the bond manually. The KISS framing constants (`FEND`/`FESC`/`TFEND`/`TFESC`) and command bytes are hand-rolled in this file, verified against RNS's own `rnodeconf.py` source rather than guessed.

Once bonded, the BLE MAC address is remembered in `rnode_state.json` so later runs skip straight to updating the config and launching `rnsd` — no USB reconnection needed. `pair_rnode()` and serial open failures are non-fatal by design: if no device/port is found, the script logs it and continues on to config + identity + launch rather than exiting, since the user may only want to (re)launch against an already-paired device or an already-correct config.

`rnode_pair.py` launches `rnsd` via `os.execv` (process replacement, not a subprocess) so that Ctrl+C and log streaming behave exactly like running `rnsd` directly. Because RNS config typically has `share_instance = Yes`, this `rnsd` and a separately-run `lxmf_messenger.py` (which calls `RNS.Reticulum()` in-process) transparently share one instance — whichever starts first opens the actual interfaces, and the other attaches as a client.

### `lxmf_messenger.py`'s threading model

RNS/LXMF deliver messages and announces from their own background transport thread, not the main thread. `Messenger._on_message` (registered via `router.register_delivery_callback`) and `Messenger.received_announce` (registered via `RNS.Transport.register_announce_handler`) both just push onto a `queue.Queue` (`notify_queue` / `presence_queue`) and return immediately; the main thread's `drain_notifications()` — polled once per keyboard-loop tick — is what actually prints alerts and fires macOS notifications. Any new code that reacts to incoming network events should follow this queue-and-drain pattern rather than doing work directly in the callback.

The keyboard UI itself (`run_keyboard_loop`) uses `tty.setcbreak` + `select.select` on stdin to read single keypresses without waiting for Enter, temporarily restoring normal terminal mode (`termios.tcsetattr`) around any sub-flow that needs real `input()` (compose, inbox, presence).

### Presence directory

`Messenger` registers itself as an `RNS.Transport` announce handler with `aspect_filter = "lxmf.delivery"`, so it hears *any* LXMF peer's announce on the network, not just people who have messaged it first. Display names are decoded from the announce's `app_data`, which LXMF encodes as `msgpack([display_name_bytes_or_None, stamp_cost, supported_functionality])` (matched against `LXMRouter.get_announce_app_data` in the installed `lxmf` package — decoding is deliberately wrapped in a broad `except Exception` since `app_data` is attacker-controlled network input). Results persist to `contacts.json`. This is the general mechanism for building any custom app/domain on Reticulum: a destination's discoverability comes from its `app_name`/aspect namespace (e.g. `"lxmf", "delivery"`), and any node can listen for announces under a namespace it doesn't otherwise participate in.

### Known upstream quirks (not bugs in this repo)

- RNS's `BackboneInterface` uses Linux/Android-only `epoll` and always fails on macOS with `module 'select' has no attribute 'epoll'`. Use `type = TCPClientInterface` instead in any config meant to run here.
- RNS's `Interface.process_announce_queue()` can log a one-time "division by zero" / "announce queue has been cleared" error right after an `RNodeInterface` comes up, before the RNode has reported its radio parameters back (its `bitrate` starts at `0`). Harmless and cosmetic; already investigated and intentionally left unpatched (see git history).

## Files that are runtime state, not source

`identity`, `rnode_state.json`, `contacts.json`, and everything under `configs/*/storage/` and `configs/*/interfaces/` are generated/mutated at runtime, not hand-edited. `configs/<name>/config` is the one hand-editable file per profile — it's a plain Reticulum config file, same format as `~/.reticulum/config`.
