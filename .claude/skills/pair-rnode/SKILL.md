---
name: pair-rnode
description: Pair a new RNode to this Mac over Bluetooth LE and wire it into an RNS config, or re-launch rnsd for an already-paired device. Use when the user wants to pair/connect/re-pair an RNode, set up rnsd for the first time, or asks why a paired device isn't showing up.
---

# Pairing an RNode

The entry point is `rnode_pair.py`. It cannot drive macOS's Bluetooth pairing dialog programmatically (no app can) — it automates everything around that one manual step, then writes the RNS config and launches `rnsd`.

## First-time pairing

```
source .venv/bin/activate
python3 rnode_pair.py
```

Connect the RNode over USB first. The script will:
1. Auto-detect the serial port (or prompt if several are found — an `[s] Skip` option is always offered if none of them are the RNode).
2. Send KISS commands to enable Bluetooth and enter pairing mode on the firmware, then read back the generated pairing PIN over serial.
3. Open System Settings > Bluetooth for you — **this part is manual**: find "RNode XXXX" and connect, entering the printed PIN when prompted.
4. Detect the bonded BLE address, append a `[[RNode BLE Interface]]` block to the resolved RNS config (backing up the config file first), create/reuse an identity, and launch `rnsd` in the foreground.

## Later runs

The paired address is remembered in `rnode_state.json` — subsequent runs skip straight to config + `rnsd`, no USB or re-pairing needed. Use `--repair` to force pairing a different device instead of reusing the saved one.

## Useful flags

| Flag | Purpose |
|---|---|
| `--repair` | Ignore saved state, pair again (e.g. a different device) |
| `--address <mac>` | Use a known BLE address directly, skip pairing/detection entirely |
| `--skip-pair` | Don't run the USB pairing wizard; just try to detect an already-bonded device |
| `--no-run` | Update config + identity but don't launch `rnsd` (useful for scripting/testing) |
| `--config <dir>` | Explicit RNS config directory — skips the interactive config-picker prompt (see the `add-config-profile` skill) |
| `--frequency` / `--bandwidth` / `--txpower` / `--spreadingfactor` / `--codingrate` | LoRa radio params written into the new interface block; default to matching whatever `[[RNode LoRa]]` (the USB entry) already uses |

## If nothing is plugged in and nothing is paired yet

That's fine — the script logs it and continues on to identity creation + launching `rnsd` rather than erroring out, since you may just want to bring RNS up against an already-correct config.

If you hit interface errors in the `rnsd` log output after this (not from `rnode_pair.py` itself), see the `debug-rns-connectivity` skill.
