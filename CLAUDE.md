# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## When adding new functionality

Each distinct capability in this repo (pairing, messaging, file transfer, git, ...) is its own standalone module/script, not a mode bolted onto an existing one. When asked to add new functionality:

1. Create a new Python module for it (following the existing scripts' shape: reuse `create_or_load_identity`/`resolve_config_dir` from `rnode_pair.py`, reuse `shared.py`'s helpers rather than re-implementing them, verify any new RNS/LXMF API calls against the installed package source or official `Examples/` rather than guessing).
2. Update `README.md` with a new section for it (what it does, how to run it, its flags, any files it creates).
3. Update this file (`CLAUDE.md`) if the new module introduces an architectural pattern, a shared convention, or a non-obvious gotcha that future work in this repo should know about — not just a one-line mention.

Don't skip the README/CLAUDE.md updates to the end "if there's time" — do them as part of the same change.

## What this repo is

Standalone Python scripts (no package/build system, cross-platform: macOS/Windows/Linux — see "Cross-platform OS integration" below) that get a Reticulum ([RNS](https://reticulum.network/)) node running with an [RNode](https://unsigned.io/rnode/) connected over Bluetooth LE, plus messaging, file transfer, and git clients on top:

- `rnode_pair.py` — pairs an RNode over BLE and wires it into an RNS config; also a shared module (`create_or_load_identity`, `resolve_config_dir`) imported by every other script here
- `lxmf_messenger.py` — interactive [LXMF](https://github.com/markqvist/LXMF) messaging client
- `file_transfer.py` — interactive file transfer client, same shape as the messenger but using `RNS.Link`/`RNS.Resource` under its own `jcomprns.filetransfer` destination namespace instead of LXMF
- `rns_git.py` — serves git repositories over Reticulum (`serve` subcommand) and provides the connect/relay logic used by the `git-remote-jcomprns` helper, under the `jcomprns.git` destination namespace
- `git-remote-jcomprns` — thin executable shim (no `.py` extension; this is the literal name git looks for on `PATH` for a `jcomprns://` remote) that hands off to `rns_git.py`'s `remote_helper_main()`
- `shared.py` — small helpers (`notify`, `load_json`/`save_json`, `human_size`) used by the interactive clients; not a script, has no `main()`

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
python3 rns_git.py serve --repos-dir <dir>   # serve bare git repos over Reticulum
git clone jcomprns://<hex-address>/<reponame>   # once git-remote-jcomprns is on PATH
```

There is no lint config, build step, or automated test suite. Verification is done by:
- `python3 -m py_compile <file>.py` for a syntax check
- ad hoc runs against a scratch config directory (pass `--config`/`--identity`/`--contacts`/`--state-file` pointing at a temp dir instead of the real `~/.reticulum` or repo state) with `unittest.mock.patch` used to script `input()` prompts and `serial.tools.list_ports.comports()` — this is necessary because real behavior involves live BLE/USB hardware and an interactive TTY that can't be part of an automated suite
- for `rns_git.py`, driving `GitServerSession` directly against a real local bare repo and a fake buffer object (`.read`/`.write`/`.flush`/`.close`) that stands in for the RNS `Buffer`, feeding it real pkt-line requests and asserting against real `git-upload-pack`/`git-receive-pack` output — this exercises all of the module's own logic (header parsing, path resolution/security, subprocess piping) without needing a live two-peer RNS link

## Architecture

### Config resolution is shared and dual-mode

`rnode_pair.py`, `lxmf_messenger.py`, `file_transfer.py`, and `rns_git.py serve` all call `resolve_config_dir()` (defined in `rnode_pair.py`) before doing anything else. If `--config` isn't passed explicitly, it interactively prompts to choose between the user's live `~/.reticulum` config and any saved profile directory under `configs/<name>/` (detected by `list_saved_configs()` — any subdirectory of `configs/` containing a `config` file). Picking a profile uses that directory as a fully isolated RNS config dir (own `storage/`, own interface state) rather than touching the live one. Passing `--config` explicitly skips the prompt. This same function is imported rather than duplicated. The one exception is the `git-remote-jcomprns` client helper, which git invokes directly with its stdin/stdout already committed to a wire protocol — see the git section below for why it uses env vars instead.

### Why `rnode_pair.py` talks raw KISS over serial

RNode's BLE stack requires OS-level Bluetooth bonding before any data can flow, and every OS only exposes that pairing dialog through its own native Bluetooth settings UI — no library can drive it programmatically, on any platform. What *can* be automated is talking to the RNode over USB serial using the same KISS commands `rnodeconf` uses (`CMD_BT_CTRL` to enable Bluetooth / enter pairing mode, `CMD_BT_PIN` to read back the generated pairing PIN), then walking the user through completing the bond manually. The KISS framing constants (`FEND`/`FESC`/`TFEND`/`TFESC`) and command bytes are hand-rolled in this file, verified against RNS's own `rnodeconf.py` source rather than guessed.

Once bonded, the BLE MAC address is remembered in `rnode_state.json` so later runs skip straight to updating the config and launching `rnsd` — no USB reconnection needed. `pair_rnode()` and serial open failures are non-fatal by design: if no device/port is found, the script logs it and continues on to config + identity + launch rather than exiting, since the user may only want to (re)launch against an already-paired device or an already-correct config.

`rnode_pair.py` launches `rnsd` via `os.execv` (process replacement, not a subprocess) so that Ctrl+C and log streaming behave exactly like running `rnsd` directly. Because RNS config typically has `share_instance = Yes`, this `rnsd` and any separately-run `lxmf_messenger.py` / `file_transfer.py` (which call `RNS.Reticulum()` in-process) transparently share one instance — whichever starts first opens the actual interfaces, and the others attach as clients. Note `RNS.Reticulum` is a hard per-process singleton (a second `RNS.Reticulum()` call in the same process raises `OSError`) — this is why the two interactive clients can't both run in one process, and why a live two-peer test needs two real processes/machines rather than one test script.

### Threading model shared by both interactive clients

RNS/LXMF deliver messages, announces, and (for `file_transfer.py`) link/resource events from their own background transport thread, not the main thread. Callbacks registered with RNS/LXMF (`Messenger._on_message`, `Messenger.received_announce`, `FileTransferNode._on_incoming_link`, `_on_resource_started`, `_on_resource_concluded`, both classes' `received_announce`) all just push onto a `queue.Queue` and return immediately; the main thread's `drain_notifications()` — polled once per keyboard-loop tick — is what actually prints alerts and fires native OS notifications via `shared.notify()`. Any new code that reacts to incoming network events should follow this queue-and-drain pattern rather than doing work directly in the callback.

The keyboard UI itself (`run_keyboard_loop`) uses `tty.setcbreak` + `select.select` on stdin to read single keypresses without waiting for Enter, temporarily restoring normal terminal mode (`termios.tcsetattr`) around any sub-flow that needs real `input()` (compose/send, inbox, presence).

### Presence directory (same pattern in both apps)

Both `Messenger` and `FileTransferNode` register themselves as an `RNS.Transport` announce handler with `aspect_filter` set to their own app's namespace (`"lxmf.delivery"` for messaging, `"jcomprns.filetransfer"` for file transfer), so each hears *any* peer's announce under that namespace on the network, not just peers who've contacted them first. This is the general mechanism for building any custom app/domain on Reticulum: a destination's discoverability comes from its `app_name`/aspect namespace, and any node can listen for announces under a namespace it doesn't otherwise participate in — the two apps' directories are independent because they're different namespaces, even when it's the same identity underneath.

Display names are decoded from each announce's `app_data`. LXMF encodes it as `msgpack([display_name_bytes_or_None, stamp_cost, supported_functionality])` (matched against `LXMRouter.get_announce_app_data` in the installed `lxmf` package); `file_transfer.py` defines its own minimal `msgpack([display_name_bytes_or_None])` since it's a custom namespace with no existing encoding to match. Both decoders are wrapped in a broad `except Exception` since `app_data` is attacker-controlled network input. Results persist to `contacts.json` / `filetransfer_contacts.json` respectively.

### `file_transfer.py`'s use of Link + Resource

Unlike LXMF (store-and-forward messages to a destination hash), file transfer needs a live `RNS.Link` to the recipient first (`RNS.Link(dest, established_callback=...)`), then an `RNS.Resource(file_handle, link, metadata={"filename": ...}, callback=...)` streamed over it. `RNS.Resource`'s `metadata` param (verified via `RNS/Resource.py`) is how the filename crosses the wire — Resources are otherwise anonymous byte streams with no filename of their own, unlike RNS's own `Examples/Filetransfer.py` which conveys the filename out-of-band via a separate request packet instead. On the receiving side, `link.set_resource_strategy(RNS.Link.ACCEPT_ALL)` auto-accepts incoming resources, and `resource.metadata` / `resource.data.read()` in the concluded callback give back the filename and bytes. Sending polls `resource.get_progress()` against a `threading.Event` set by the completion callback, rather than a fixed sleep loop, so it exits the instant the transfer concludes.

### `rns_git.py`'s use of Link + Buffer (Channel), and the git remote-helper protocol

Unlike file transfer's one-shot `Resource`, git needs a full-duplex, back-and-forth pipe: `channel = link.get_channel()` then `RNS.Buffer.create_bidirectional_buffer(0, 0, channel, ready_callback)` (verified against RNS's own `Examples/Buffer.py`) gives a real Python file-like object over the link. Both ends use stream_id `0` for both directions — stream IDs are scoped per-*receiver*, so this doesn't collide. This is exactly the same trick `ssh` uses for git: give git's own `git-upload-pack`/`git-receive-pack` a bidirectional byte pipe and they speak their existing wire protocol over it unmodified — no git-specific protocol work needed here, only the pipe and (on the client) the minimal `connect`-capability handshake from git's [remote-helper protocol](https://git-scm.com/docs/gitremote-helpers) (`git-remote-jcomprns` → `capabilities` → `connect <service>` → blank-line ack → transparent relay).

Because the RNS server doesn't know which repo/service a connecting client wants until it says so, the client sends a one-line plaintext header (`"<service> <reponame>\n"`) as the *first* bytes over the buffer, before either side starts speaking git's actual protocol — analogous to how the repo path is embedded in the ssh command line rather than git's wire protocol itself. `GitServerSession._on_ready` buffers incoming bytes until it sees that newline, then spawns `git-upload-pack`/`git-receive-pack` against the resolved repo path.

**Gotcha that cost a debugging round when this was written**: piping a subprocess's `stdout` (or `stdin_raw`) with `.read(65536)` looks right but isn't — `io.BufferedReader.read(size)` blocks trying to *fill* the requested size before returning, which stalls interactive back-and-forth protocols like git's (small negotiation packets, not 64KB blobs). Use `.read1(size)` instead, which returns whatever's currently available without waiting to fill the buffer. This was caught by testing `GitServerSession` against a real `git-upload-pack` process and seeing zero bytes come back until this was fixed. Apply the same care (`read1`, or the `ready_callback`-driven pattern RNS's own `Buffer` example uses for the *receiving* side) to any future code that pipes a live/interactive stream — it's a "looks correct, silently stalls" trap, not a crash.

The `git-remote-jcomprns` shim is invoked directly by git with its own stdin/stdout already committed to the remote-helper protocol, so it can't use the interactive `resolve_config_dir()` prompt (there's no room for a human prompt in that stream, and stdin is git's protocol channel, not a keyboard). It reads `JCOMPRNS_CONFIG`/`JCOMPRNS_IDENTITY` env vars instead, defaulting to the live config and shared identity.

### Cross-platform OS integration

`pyserial` and RNS itself are already fully cross-platform, so the core pairing/config/identity/messaging/file-transfer/git logic needs no OS branching at all. Only the handful of places that shell out to an OS-native tool need per-platform dispatch, and they all follow the same shape: branch on `platform.system()` (`"Darwin"` / `"Windows"` / `"Linux"`), one private `_thing_<os>()` implementation per branch, each wrapped so failures degrade to "couldn't do this automatically, here's what to run/check manually" rather than crashing:

- `shared.notify()` — `osascript` (macOS) / a PowerShell WinRT toast script (Windows, no extra modules needed) / `notify-send` (Linux, part of `libnotify`). Untrusted content (message previews, peer names) is never interpolated into the macOS AppleScript or the Windows PowerShell script text — it's escaped (macOS) or passed as a bound script `param()` via a separate argv entry (Windows), since both are real script-injection surfaces when the content comes from the network.
- `rnode_pair.py`'s `find_bonded_rnode_address()` — `system_profiler SPBluetoothDataType -json` (macOS, JSON) / PowerShell `Get-PnpDevice -Class Bluetooth` (Windows, MAC extracted from the `BTHLE\DEV_XXXXXXXXXXXX\...` instance ID via `_extract_mac_address()`) / `bluetoothctl devices Paired` (Linux, BlueZ). Same for `open_bluetooth_settings()` and the printed instructions (`bluetooth_settings_label()`).
- macOS is the only platform this repo has actually been run on. The Windows/Linux branches are implemented against each OS's standard, documented tooling and covered by unit tests that mock `platform.system()`/`subprocess.run`/`shutil.which` and feed fabricated-but-realistic tool output (e.g. a sample `Get-PnpDevice` InstanceId line, a sample `bluetoothctl devices Paired` line) — this verifies the dispatch and parsing logic, but isn't the same as having run on real Windows/Linux hardware. Flag this honestly rather than claiming full verification if asked about platform support.

### Known upstream quirks (not bugs in this repo)

- RNS's `BackboneInterface` uses Linux/Android-only `epoll` and always fails on macOS with `module 'select' has no attribute 'epoll'`. Use `type = TCPClientInterface` instead in any config meant to run here.
- RNS's `Interface.process_announce_queue()` can log a one-time "division by zero" / "announce queue has been cleared" error right after an `RNodeInterface` comes up, before the RNode has reported its radio parameters back (its `bitrate` starts at `0`). Harmless and cosmetic; already investigated and intentionally left unpatched (see git history).

## Files that are runtime state, not source

`identity`, `rnode_state.json`, `contacts.json`, `filetransfer_contacts.json`, `received_files/`, `received_files.json`, and everything under `configs/*/storage/` and `configs/*/interfaces/` are generated/mutated at runtime, not hand-edited. `configs/<name>/config` is the one hand-editable file per profile — it's a plain Reticulum config file, same format as `~/.reticulum/config`.
