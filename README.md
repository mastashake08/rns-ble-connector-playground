# jcomprns

Pair an [RNode](https://unsigned.io/rnode/) to this machine over Bluetooth LE and use it for LXMF messaging, file transfer, and `git clone`-able repositories — all over [Reticulum (RNS)](https://reticulum.network/).

Installs as a normal pip package with real terminal commands, the same way `rns` gives you `rnsd`/`rnodeconf`. Published on PyPI as [`jcomprns`](https://pypi.org/project/jcomprns/):

```
python3 -m venv .venv
source .venv/bin/activate
pip install jcomprns
```

Working on this repo directly (not just using it)? Install it in editable mode from a local checkout instead, so changes to the source take effect immediately:

```
pip install -e .
```

Either way, this installs five commands onto your `PATH`: `jcomprns-pair`, `jcomprns-chat`, `jcomprns-send`, `jcomprns-git`, and `git-remote-jcomprns` (used automatically by `git`, you never run it directly).

All of this app's state — identity, remembered pairing, presence directories, received files, saved config profiles — lives in `~/.jcomprns/`, independent of wherever pip happens to install the package (same idea as RNS's own `~/.reticulum`).

## Why this exists

RNode's BLE firmware requires a bonded (secure) connection before any data can flow, and the OS only lets you create that bond through its own Bluetooth pairing UI — no app, including this one, can drive that dialog programmatically. `jcomprns-pair` automates everything *around* that manual step:

1. Talks to the RNode over USB serial (the same KISS commands `rnodeconf` uses) to switch on Bluetooth and put the device into pairing mode.
2. Reads back the pairing PIN the firmware generates and opens your OS's Bluetooth settings for you.
3. Once you've completed the bond, detects the RNode's Bluetooth address and adds a `[[RNode BLE Interface]]` block to your Reticulum config (`port = ble://<address>`).
4. Creates a Reticulum identity (or reuses one you already have).
5. Launches `rnsd` in the foreground.

RNS handles the actual data link over BLE itself from there (via `bleak`) — this project only exists to get the one-time OS-level pairing and config wiring out of the way, plus the messaging/file-transfer/git tools built on top.

Once a device has been paired, its address is remembered in `~/.jcomprns/rnode_state.json`, so every run after the first skips straight to updating the config and launching `rnsd` — no re-pairing needed.

## Verbose / debug output

By default, things that are handled silently on purpose — a best-effort OS notification failing, a corrupt state file falling back to a default, garbage announce data from the network being ignored — stay silent, so normal runs stay clean. Pass `-v`/`--verbose` to any command to see that detail instead:

```
jcomprns-pair --verbose
jcomprns-chat --verbose
jcomprns-send --verbose
jcomprns-git serve --verbose
```

This also bumps RNS's own internal logging to debug level for that run. It never changes behavior — only whether these diagnostics get printed (to stderr).

For `git-remote-jcomprns`, which git invokes directly with no room for extra flags, set the `JCOMPRNS_VERBOSE=1` environment variable instead.

## Platform support

The core pairing flow (talking to the RNode over USB/KISS, RNS config, identity, `rnsd`) is fully cross-platform — it's all `pyserial`/RNS, which already work identically on macOS, Windows, and Linux. Two pieces are inherently OS-specific and are implemented per-platform:

| | macOS | Windows | Linux |
|---|---|---|---|
| Open Bluetooth settings | `open x-apple.systempreferences:...` | `ms-settings:bluetooth` | first available of `gnome-control-center`, `blueman-manager`, `kcmshell5` |
| Detect the bonded RNode's address | `system_profiler SPBluetoothDataType` | PowerShell `Get-PnpDevice -Class Bluetooth` | `bluetoothctl devices Paired` (BlueZ) |

**macOS is the primary tested platform** (this project was built and verified there). The Windows and Linux paths are implemented against each OS's standard, documented tooling and covered by unit tests with fabricated realistic output, but haven't been run on real Windows/Linux hardware. If auto-detection fails on your platform, the script tells you what to run manually (or pass `--address` to skip detection entirely once you know the address).

Native OS notifications (used by `jcomprns-chat` and `jcomprns-send`) work the same way: `osascript` on macOS, a PowerShell WinRT toast on Windows (no extra modules needed), `notify-send` on Linux (part of `libnotify`, present on most desktop distros). If the relevant tool isn't available, notifications are silently skipped — nothing else in the app depends on them.

## Pairing an RNode

Connect the RNode over USB, then:

```
jcomprns-pair
```

First run: walks you through USB → BLE pairing, then writes the config, creates an identity, and launches `rnsd`.

Later runs: automatically reuses the saved address, updates config/identity if needed, and launches `rnsd` — no USB connection required.

Useful flags:

| Flag | Purpose |
|---|---|
| `--repair` | Ignore the saved address and pair a (different) device again |
| `--address <mac>` | Use a specific BLE address directly, skipping pairing/detection |
| `--no-run` | Update config + identity but don't launch `rnsd` |
| `--config <dir>` | Reticulum config directory; skips the startup config prompt if given |
| `--identity <path>` | Identity file to create/reuse (default `~/.jcomprns/identity`) |
| `--state-file <path>` | Where the paired address is remembered (default `~/.jcomprns/rnode_state.json`) |
| `--frequency` / `--bandwidth` / `--txpower` / `--spreadingfactor` / `--codingrate` | LoRa radio parameters written into the interface block |

Run `jcomprns-pair --help` for the full list.

## Files this creates (all under `~/.jcomprns/`)

- `rnode_state.json` — remembers the paired RNode's BLE address between runs
- `identity` — Reticulum identity file (keep this private; anyone with it can decrypt traffic for it), shared by all four tools so your address stays consistent
- `contacts.json` — the presence directory of LXMF peers seen announcing on the network (created by `jcomprns-chat`)
- `filetransfer_contacts.json` — the presence directory of file-transfer peers seen announcing on the network (created by `jcomprns-send`)
- `received_files/` and `received_files.json` — incoming files and the manifest of what was received, when, and from where (created by `jcomprns-send`)
- `configs/<name>/` — saved Reticulum config profiles (see "Config profiles" below)
- `~/.reticulum/config` — gets a `[[RNode BLE Interface]]` block appended (existing interfaces are left untouched); a timestamped backup is made before every edit

## Messaging (LXMF)

`jcomprns-chat` is a small interactive [LXMF](https://github.com/markqvist/LXMF) messaging client that runs over the same Reticulum setup. It reuses the identity created by `jcomprns-pair`, so your LXMF address stays the same across every tool here.

```
jcomprns-chat
```

It brings up Reticulum itself (attaching to `rnsd` if it's already running as the shared instance, or opening the configured interfaces directly if not), then drops into a single-keypress UI:

- **M** — compose a message: paste a recipient's LXMF address (hex), optionally a title, and the message body
- **I** — open the inbox: lists received messages and lets you pick one to reply to
- **P** — open the presence directory: lists every LXMF peer seen announcing on the network, and lets you pick one to message directly
- **Q** — quit

Incoming messages trigger a terminal alert (with a bell) and a native OS notification. Your own LXMF address is printed on startup — that's what you give other people so they can message you.

Flags: `--config`, `--identity` (same meaning as in `jcomprns-pair`), `--display-name` (shown to peers when you announce), `--stamp-cost` (proof-of-work senders must pay you before delivery; default `0`), `--contacts <path>` (where the presence directory is saved; default `~/.jcomprns/contacts.json`), `--announce-interval <minutes>` (periodically re-announce yourself so others can discover you; default `0` = announce once at startup only).

### Presence directory

Reticulum destinations are namespaced by an app name plus aspects (e.g. LXMF uses `lxmf.delivery`), and any node in the network can listen for announces under a given namespace without already knowing who's out there. `jcomprns-chat` registers a listener for `lxmf.delivery` announces network-wide, so it builds up a contact list of every LXMF peer it's seen — not just people who've messaged you first.

Each contact records their address, display name (if they set one), and first/last seen times, persisted to `contacts.json`. New contacts trigger the same terminal alert as an incoming message. From the **P** screen you can jump straight into composing a message to any saved contact, or trigger `[A]` to re-announce yourself so others can discover you back.

This is mutual: for two peers to find each other, both need to have announced at some point since either was last online. Use `--announce-interval` if you want that to happen automatically instead of only at startup.

## File transfer

`jcomprns-send` is the same idea as the messenger, but for sending files instead of text. It runs over the same Reticulum setup and can share the same identity, but registers its own destination under a different namespace (`jcomprns.filetransfer` vs LXMF's `lxmf.delivery`) — so it has its own address, its own presence directory, and its own contacts, even though it's the same identity underneath.

```
jcomprns-send
```

- **S** — send a file: paste a recipient's address (hex) and a local file path
- **R** — list received files: shows what's been received, when, from whom, and where it was saved on disk (under `~/.jcomprns/received_files/`)
- **P** — presence directory: same mechanism as the messenger's, scoped to file-transfer peers; pick one to send a file directly, or press `[A]` to re-announce yourself
- **Q** — quit

Under the hood this uses `RNS.Link` + `RNS.Resource`, which is Reticulum's built-in mechanism for moving arbitrary data with automatic compression, chunking, and integrity checking — a link is established with the recipient first, then the file streams over it with a live progress percentage. Per RNS's own guidance, Resources aren't recommended for very large files (compression/hashing can outrun the receiver's timeout on slow links); this is intended for the kind of file sizes that make sense over a LoRa-connected RNode, not bulk transfer.

Flags: `--config`, `--identity`, `--display-name`, `--announce-interval` (same meaning as in `jcomprns-chat`), `--received-dir` (where incoming files are saved; default `~/.jcomprns/received_files`), `--manifest <path>` (where the received-files log is kept; default `~/.jcomprns/received_files.json`), `--contacts <path>` (default `~/.jcomprns/filetransfer_contacts.json`).

## Git over Reticulum

`jcomprns-git` lets you `git clone`/`fetch`/`push` a repository over Reticulum, using completely normal git commands — no different from an `ssh://` remote from git's point of view.

This works the same way `ssh` does for git: git already knows how to speak its own wire protocol over an arbitrary bidirectional byte stream (that's literally what happens over ssh — `ssh host git-upload-pack '/repo'` pipes git's pack protocol over the ssh channel). This module provides that same stream over an RNS `Link` using `RNS.Buffer.create_bidirectional_buffer()`, and spawns the real `git-upload-pack`/`git-receive-pack` binaries on the serving side. Git itself needs no changes; the `git-remote-jcomprns` command (installed automatically alongside everything else) is what makes `git` recognize the `jcomprns://` URL scheme.

### Serving repositories

```
jcomprns-git serve --repos-dir /path/to/repos
```

`--repos-dir` should contain one or more bare repositories (e.g. `myrepo.git`, created with `git init --bare`). The command prints your address and the exact URL to give clients:

```
Your jcomprns git address: <hex-address>
Share this with clients as: git clone jcomprns://<hex-address>/<reponame>
```

Press Enter to announce again, Ctrl+C to quit. Flags: `--config`, `--identity` (same meaning as elsewhere), `--announce-interval <minutes>`.

### Cloning / fetching / pushing

No setup needed beyond installing this package — `pip install -e .` already put `git-remote-jcomprns` on your `PATH`, and git finds it automatically for any `jcomprns://` remote:

```
git clone jcomprns://<hex-address>/<reponame>
git remote add origin jcomprns://<hex-address>/<reponame>   # for an existing repo
git fetch
git push
```

By default the client uses your live `~/.reticulum` config and the shared `~/.jcomprns/identity` file — since git invokes the helper directly with its own stdin/stdout already committed to the wire protocol, there's no interactive config-picker prompt here (unlike the other commands). Override with the `JCOMPRNS_CONFIG` and `JCOMPRNS_IDENTITY` environment variables if you need a specific config profile or identity. Having `rnsd` already running in the background (via `jcomprns-pair`) is recommended if you'll be doing git operations repeatedly, so each one doesn't have to bring up interfaces from scratch.

### Security notes

The server only serves directories that already exist directly under `--repos-dir`; requests for repo names containing `..` or resolving outside that directory are rejected. There's no authentication beyond Reticulum's own identity/encryption — anyone with the server's address can attempt `upload-pack` (read) and `receive-pack` (push) against any repo you're serving. Don't serve anything you wouldn't hand out to anyone who obtains the address.

## Config profiles (`~/.jcomprns/configs/`)

`jcomprns-pair`, `jcomprns-chat`, `jcomprns-send`, and `jcomprns-git serve` all prompt at startup (the `git-remote-jcomprns` client helper does not — see above):

```
Which Reticulum config do you want to use?
  [0] Your live config (~/.reticulum)
  [1] default  (/Users/you/.jcomprns/configs/default)
Choice [0]:
```

- **[0]** (or just pressing Enter) uses your live `~/.reticulum` config, same as before.
- Any other number uses that saved profile directory under `~/.jcomprns/configs/` as the Reticulum config directory for this run (its own `config` file, and its own `storage/`/identity cache, isolated from your live setup).

Pass `--config <dir>` on the command line to skip the prompt entirely and use that directory directly (scripting/automation).

To save a new profile, copy a working `config` file into `~/.jcomprns/configs/<name>/config` — it'll show up in the list automatically.

## Releasing a new version (already published to PyPI)

[`jcomprns`](https://pypi.org/project/jcomprns/) is live on PyPI — `pip install jcomprns` works for anyone, no repo checkout needed. To ship a new version:

1. Bump `version` in `pyproject.toml` (PyPI rejects re-uploading an existing version number, even with identical contents).
2. Build fresh artifacts (delete old ones first so `twine` doesn't try to re-upload a stale version):
   ```
   rm -rf dist build src/jcomprns.egg-info
   python3 -m build
   ```
   Optionally sanity-check first against TestPyPI (`python3 -m twine upload --repository testpypi dist/*`, then `pip install --index-url https://test.pypi.org/simple/ jcomprns` to confirm).
3. Upload:
   ```
   python3 -m twine upload dist/*
   ```
   Use `__token__` as the username and a PyPI API token (account settings → API tokens) as the password when prompted.
