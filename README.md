# RNS BLE Connector Playground

Pairs an [RNode](https://unsigned.io/rnode/) to this Mac over Bluetooth LE and wires it into [Reticulum (RNS)](https://reticulum.network/) as a `RNodeInterface`.

## Why this exists

RNode's BLE firmware requires a bonded (secure) connection before any data can flow, and macOS only lets you create that bond through its own Bluetooth pairing UI — no app, including this one, can drive that dialog programmatically. What this script automates is everything *around* that manual step:

1. Talks to the RNode over USB serial (the same KISS commands `rnodeconf` uses) to switch on Bluetooth and put the device into pairing mode.
2. Reads back the pairing PIN the firmware generates and opens System Settings > Bluetooth for you.
3. Once you've completed the bond, detects the RNode's Bluetooth address and adds a `[[RNode BLE Interface]]` block to your Reticulum config (`port = ble://<address>`).
4. Creates a Reticulum identity (or reuses one you already have).
5. Launches `rnsd` in the foreground.

RNS handles the actual data link over BLE itself from there (via `bleak`) — this project only exists to get the one-time OS-level pairing and config wiring out of the way.

Once a device has been paired, its address is remembered in `rnode_state.json`, so every run after the first skips straight to updating the config and launching `rnsd` — no re-pairing needed.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Connect the RNode over USB, then:

```
source .venv/bin/activate
python3 rnode_pair.py
```

First run: walks you through USB → BLE pairing, then writes the config, creates an identity, and launches `rnsd`.

Later runs: automatically reuses the saved address, updates config/identity if needed, and launches `rnsd` — no USB connection required.

Useful flags:

| Flag | Purpose |
|---|---|
| `--repair` | Ignore the saved address and pair a (different) device again |
| `--address <mac>` | Use a specific BLE address directly, skipping pairing/detection |
| `--no-run` | Update config + identity but don't launch `rnsd` |
| `--config <dir>` | Reticulum config directory (default `~/.reticulum`) |
| `--identity <path>` | Identity file to create/reuse (default `./identity`) |
| `--state-file <path>` | Where the paired address is remembered (default `./rnode_state.json`) |
| `--frequency` / `--bandwidth` / `--txpower` / `--spreadingfactor` / `--codingrate` | LoRa radio parameters written into the interface block |

Run `python3 rnode_pair.py --help` for the full list.

## Files this creates

- `rnode_state.json` — remembers the paired RNode's BLE address between runs
- `identity` — Reticulum identity file (keep this private; anyone with it can decrypt traffic for it)
- `~/.reticulum/config` — gets a `[[RNode BLE Interface]]` block appended (existing interfaces are left untouched); a timestamped backup is made before every edit

## Messaging (LXMF)

`lxmf_messenger.py` is a small interactive [LXMF](https://github.com/markqvist/LXMF) messaging client that runs over the same Reticulum setup. It reuses the identity created by `rnode_pair.py`, so your LXMF address stays the same across both tools.

```
source .venv/bin/activate
python3 lxmf_messenger.py
```

It brings up Reticulum itself (attaching to `rnsd` if it's already running as the shared instance, or opening the configured interfaces directly if not), then drops into a single-keypress UI:

- **M** — compose a message: paste a recipient's LXMF address (hex), optionally a title, and the message body
- **I** — open the inbox: lists received messages and lets you pick one to reply to
- **Q** — quit

Incoming messages trigger a terminal alert (with a bell) and a native macOS notification. Your own LXMF address is printed on startup — that's what you give other people so they can message you.

Flags: `--config`, `--identity` (same meaning as in `rnode_pair.py`), `--display-name` (shown to peers when you announce), `--stamp-cost` (proof-of-work senders must pay you before delivery; default `0`).
