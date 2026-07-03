# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Standalone Python scripts (no package/build system) that get a Reticulum ([RNS](https://reticulum.network/)) node running on this Mac with an [RNode](https://unsigned.io/rnode/) connected over Bluetooth LE, plus messaging and file transfer clients on top:

- `rnode_pair.py` — pairs an RNode over BLE and wires it into an RNS config; also a shared module (`create_or_load_identity`, `resolve_config_dir`) imported by the other two scripts
- `lxmf_messenger.py` — interactive [LXMF](https://github.com/markqvist/LXMF) messaging client
- `file_transfer.py` — interactive file transfer client, same shape as the messenger but using `RNS.Link`/`RNS.Resource` under its own `bleconnector.filetransfer` destination namespace instead of LXMF
- `shared.py` — small helpers (`notify_macos`, `load_json`/`save_json`, `human_size`) used by both interactive clients; not a script, has no `main()`

## Commands

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Inside the venv, use `python3 -m pip install <pkg>` rather than a bare `pip install` — on this machine `pip` is shell-aliased to a system pip3 that bypasses the venv, so `python3 -m pip` is the reliable way to actually install into `.venv`.

Run the entry points directly:
```
python3 rnode_pair.py        # pair a new RNode, or re-run to just update config + launch rnsd
python3 lxmf_messenger.py    # interactive messaging client (M compose, I inbox, P presence, Q quit)
python3 file_transfer.py     # interactive file transfer client (S send, R received files, P presence, Q quit)
```

There is no lint config, build step, or automated test suite. Verification is done by:
- `python3 -m py_compile <file>.py` for a syntax check
- ad hoc runs against a scratch config directory (pass `--config`/`--identity`/`--contacts`/`--state-file` pointing at a temp dir instead of the real `~/.reticulum` or repo state) with `unittest.mock.patch` used to script `input()` prompts and `serial.tools.list_ports.comports()` — this is necessary because real behavior involves live BLE/USB hardware and an interactive TTY that can't be part of an automated suite

## Architecture

### Config resolution is shared and dual-mode

All three scripts' `main()` call `resolve_config_dir()` (defined in `rnode_pair.py`) before doing anything else. If `--config` isn't passed explicitly, it interactively prompts to choose between the user's live `~/.reticulum` config and any saved profile directory under `configs/<name>/` (detected by `list_saved_configs()` — any subdirectory of `configs/` containing a `config` file). Picking a profile uses that directory as a fully isolated RNS config dir (own `storage/`, own interface state) rather than touching the live one. Passing `--config` explicitly skips the prompt. This same function is imported into `lxmf_messenger.py` and `file_transfer.py` rather than duplicated.

### Why `rnode_pair.py` talks raw KISS over serial

RNode's BLE stack requires OS-level Bluetooth bonding before any data can flow, and macOS only exposes that pairing dialog through System Settings — no library can drive it programmatically. What *can* be automated is talking to the RNode over USB serial using the same KISS commands `rnodeconf` uses (`CMD_BT_CTRL` to enable Bluetooth / enter pairing mode, `CMD_BT_PIN` to read back the generated pairing PIN), then walking the user through completing the bond manually. The KISS framing constants (`FEND`/`FESC`/`TFEND`/`TFESC`) and command bytes are hand-rolled in this file, verified against RNS's own `rnodeconf.py` source rather than guessed.

Once bonded, the BLE MAC address is remembered in `rnode_state.json` so later runs skip straight to updating the config and launching `rnsd` — no USB reconnection needed. `pair_rnode()` and serial open failures are non-fatal by design: if no device/port is found, the script logs it and continues on to config + identity + launch rather than exiting, since the user may only want to (re)launch against an already-paired device or an already-correct config.

`rnode_pair.py` launches `rnsd` via `os.execv` (process replacement, not a subprocess) so that Ctrl+C and log streaming behave exactly like running `rnsd` directly. Because RNS config typically has `share_instance = Yes`, this `rnsd` and any separately-run `lxmf_messenger.py` / `file_transfer.py` (which call `RNS.Reticulum()` in-process) transparently share one instance — whichever starts first opens the actual interfaces, and the others attach as clients. Note `RNS.Reticulum` is a hard per-process singleton (a second `RNS.Reticulum()` call in the same process raises `OSError`) — this is why the two interactive clients can't both run in one process, and why a live two-peer test needs two real processes/machines rather than one test script.

### Threading model shared by both interactive clients

RNS/LXMF deliver messages, announces, and (for `file_transfer.py`) link/resource events from their own background transport thread, not the main thread. Callbacks registered with RNS/LXMF (`Messenger._on_message`, `Messenger.received_announce`, `FileTransferNode._on_incoming_link`, `_on_resource_started`, `_on_resource_concluded`, both classes' `received_announce`) all just push onto a `queue.Queue` and return immediately; the main thread's `drain_notifications()` — polled once per keyboard-loop tick — is what actually prints alerts and fires macOS notifications. Any new code that reacts to incoming network events should follow this queue-and-drain pattern rather than doing work directly in the callback.

The keyboard UI itself (`run_keyboard_loop`) uses `tty.setcbreak` + `select.select` on stdin to read single keypresses without waiting for Enter, temporarily restoring normal terminal mode (`termios.tcsetattr`) around any sub-flow that needs real `input()` (compose/send, inbox, presence).

### Presence directory (same pattern in both apps)

Both `Messenger` and `FileTransferNode` register themselves as an `RNS.Transport` announce handler with `aspect_filter` set to their own app's namespace (`"lxmf.delivery"` for messaging, `"bleconnector.filetransfer"` for file transfer), so each hears *any* peer's announce under that namespace on the network, not just peers who've contacted them first. This is the general mechanism for building any custom app/domain on Reticulum: a destination's discoverability comes from its `app_name`/aspect namespace, and any node can listen for announces under a namespace it doesn't otherwise participate in — the two apps' directories are independent because they're different namespaces, even when it's the same identity underneath.

Display names are decoded from each announce's `app_data`. LXMF encodes it as `msgpack([display_name_bytes_or_None, stamp_cost, supported_functionality])` (matched against `LXMRouter.get_announce_app_data` in the installed `lxmf` package); `file_transfer.py` defines its own minimal `msgpack([display_name_bytes_or_None])` since it's a custom namespace with no existing encoding to match. Both decoders are wrapped in a broad `except Exception` since `app_data` is attacker-controlled network input. Results persist to `contacts.json` / `filetransfer_contacts.json` respectively.

### `file_transfer.py`'s use of Link + Resource

Unlike LXMF (store-and-forward messages to a destination hash), file transfer needs a live `RNS.Link` to the recipient first (`RNS.Link(dest, established_callback=...)`), then an `RNS.Resource(file_handle, link, metadata={"filename": ...}, callback=...)` streamed over it. `RNS.Resource`'s `metadata` param (verified via `RNS/Resource.py`) is how the filename crosses the wire — Resources are otherwise anonymous byte streams with no filename of their own, unlike RNS's own `Examples/Filetransfer.py` which conveys the filename out-of-band via a separate request packet instead. On the receiving side, `link.set_resource_strategy(RNS.Link.ACCEPT_ALL)` auto-accepts incoming resources, and `resource.metadata` / `resource.data.read()` in the concluded callback give back the filename and bytes. Sending polls `resource.get_progress()` against a `threading.Event` set by the completion callback, rather than a fixed sleep loop, so it exits the instant the transfer concludes.

### Known upstream quirks (not bugs in this repo)

- RNS's `BackboneInterface` uses Linux/Android-only `epoll` and always fails on macOS with `module 'select' has no attribute 'epoll'`. Use `type = TCPClientInterface` instead in any config meant to run here.
- RNS's `Interface.process_announce_queue()` can log a one-time "division by zero" / "announce queue has been cleared" error right after an `RNodeInterface` comes up, before the RNode has reported its radio parameters back (its `bitrate` starts at `0`). Harmless and cosmetic; already investigated and intentionally left unpatched (see git history).

## Files that are runtime state, not source

`identity`, `rnode_state.json`, `contacts.json`, `filetransfer_contacts.json`, `received_files/`, `received_files.json`, and everything under `configs/*/storage/` and `configs/*/interfaces/` are generated/mutated at runtime, not hand-edited. `configs/<name>/config` is the one hand-editable file per profile — it's a plain Reticulum config file, same format as `~/.reticulum/config`.
