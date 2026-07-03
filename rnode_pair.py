#!/usr/bin/env python3
"""
Pairs an RNode to this Mac over Bluetooth LE, then wires it into RNS.

RNode's BLE stack requires a bonded (secure) connection before RNS can talk
to it, and that bond can only be created through macOS's own Bluetooth
pairing UI -- no third-party app can drive that dialog. What this script
automates is everything around that: talk to the RNode over its USB serial
port (via the same KISS commands rnodeconf uses) to switch on Bluetooth and
put it into pairing mode, read back the pairing PIN the firmware generates,
walk you through completing the bond in System Settings, then add a
`ble://<address>` RNodeInterface to your Reticulum config, create a
Reticulum identity if you don't already have one, and launch rnsd.

Once paired, the RNode's address is remembered in rnode_state.json, so later
runs skip straight to config + launch. Pass --repair to pair a different
device instead.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

CMD_BT_CTRL = 0x46
CMD_BT_PIN = 0x62

BT_CTRL_DISABLE = 0x00
BT_CTRL_ENABLE = 0x01
BT_CTRL_PAIR = 0x02

RNODE_PORT_HINTS = ("usbserial", "usbmodem", "SLAB", "CP210", "CH340", "CH9102", "wchusbserial")

LIVE_CONFIG_DIR = "~/.reticulum"
CONFIGS_DIR = Path(__file__).parent / "configs"


def list_saved_configs(configs_dir=CONFIGS_DIR):
    configs_dir = Path(configs_dir)
    if not configs_dir.exists():
        return []
    return sorted(p.name for p in configs_dir.iterdir() if p.is_dir() and (p / "config").exists())


def resolve_config_dir(explicit_config, configs_dir=CONFIGS_DIR):
    """Return the RNS config directory to use, prompting the user to pick
    between their live config and a saved one under configs/ if --config
    wasn't given explicitly."""
    if explicit_config:
        return explicit_config

    profiles = list_saved_configs(configs_dir)
    if not profiles:
        return LIVE_CONFIG_DIR

    print("Which Reticulum config do you want to use?")
    print(f"  [0] Your live config ({LIVE_CONFIG_DIR})")
    for i, name in enumerate(profiles, start=1):
        print(f"  [{i}] {name}  (configs/{name})")

    choice = input("Choice [0]: ").strip()
    if choice and choice.isdigit() and 1 <= int(choice) <= len(profiles):
        return str(configs_dir / profiles[int(choice) - 1])

    if choice and choice != "0":
        print("Invalid choice, using your live config.")
    return LIVE_CONFIG_DIR


def find_rnode_port():
    ports = list(list_ports.comports())
    candidates = [p for p in ports if any(hint.lower() in (p.device + p.description).lower() for hint in RNODE_PORT_HINTS)]
    if not candidates:
        candidates = ports

    if len(candidates) == 1:
        return candidates[0].device

    if not candidates:
        return None

    print("Multiple serial ports found, please choose the RNode:")
    for i, p in enumerate(candidates):
        print(f"  [{i}] {p.device}  ({p.description})")
    print("  [s] Skip -- continue without pairing")

    while True:
        choice = input("Port number: ").strip().lower()
        if choice in ("s", "skip"):
            return None
        if choice.isdigit() and int(choice) in range(len(candidates)):
            return candidates[int(choice)].device
        print(f"Enter a number from 0-{len(candidates) - 1}, or 's' to skip.")


def send_kiss_command(ser, command, payload_byte):
    frame = bytes([FEND, command, payload_byte, FEND])
    written = ser.write(frame)
    if written != len(frame):
        raise IOError("Short write while sending KISS command to RNode")


def read_bt_pin(ser, timeout_s):
    deadline = time.time() + timeout_s
    in_frame = False
    escape = False
    command = None
    payload = bytearray()

    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        for byte in chunk:
            if byte == FEND:
                if in_frame and command == CMD_BT_PIN and len(payload) == 4:
                    return int.from_bytes(payload, "big")
                in_frame = True
                command = None
                escape = False
                payload = bytearray()
                continue

            if not in_frame:
                continue

            if command is None:
                command = byte
                continue

            if byte == FESC:
                escape = True
                continue

            if escape:
                byte = FEND if byte == TFEND else (FESC if byte == TFESC else byte)
                escape = False

            payload.append(byte)

    return None


def open_bluetooth_settings():
    try:
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.bluetooth"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def find_bonded_rnode_address():
    try:
        out = subprocess.run(
            ["system_profiler", "SPBluetoothDataType", "-json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        data = json.loads(out)
    except ValueError:
        return None

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.startswith("RNode") and isinstance(value, dict):
                    address = value.get("device_address")
                    if address:
                        return address.replace("-", ":").lower()
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(data)


def pair_rnode(args):
    port = args.port or find_rnode_port()
    if not port:
        print("No serial port selected -- plug in the RNode over USB to pair it, or pass --port explicitly.")
        print("Continuing without pairing.")
        return None

    print(f"Connecting to RNode on {port} @ {args.baud}...")
    try:
        with serial.Serial(port, args.baud, timeout=0.5) as ser:
            time.sleep(0.5)  # let the port settle before writing

            print("Enabling Bluetooth on the RNode...")
            send_kiss_command(ser, CMD_BT_CTRL, BT_CTRL_ENABLE)
            time.sleep(0.5)

            print("Putting the RNode into pairing mode...")
            send_kiss_command(ser, CMD_BT_CTRL, BT_CTRL_PAIR)

            print(f"Waiting up to {args.pin_timeout:.0f}s for the RNode to report its pairing PIN...")
            pin = read_bt_pin(ser, args.pin_timeout)
    except (serial.SerialException, IOError) as e:
        print(f"Couldn't talk to the RNode over {port}: {e}")
        print("Continuing without pairing.")
        return None

    print()
    if pin is not None:
        print(f"Pairing PIN: {pin:06d}")
    else:
        print("The RNode didn't report a PIN over serial in time (some units show it on an onboard display instead).")
        print("You can still proceed -- macOS will show whatever PIN the device presents during pairing.")

    print()
    print("Now finish pairing on the Mac:")
    print("  1. Opening System Settings > Bluetooth...")
    open_bluetooth_settings()
    print("  2. Find the device named 'RNode XXXX' under nearby devices and click Connect.")
    if pin is not None:
        print(f"  3. When macOS prompts for a passkey, enter: {pin:06d}")
    else:
        print("  3. When macOS prompts for a passkey, enter the PIN the RNode displays.")
    print("  4. Confirm the pairing on both sides if prompted.")
    print()
    input("Press Enter once pairing is complete in System Settings...")

    address = find_bonded_rnode_address()
    print()
    if address:
        print(f"Paired. RNode BLE address: {address}")
    else:
        print("Couldn't auto-detect the RNode's Bluetooth address.")
        print("Look it up via the (i) button next to the device in System Settings > Bluetooth,")
        print("or run: system_profiler SPBluetoothDataType")
    return address


DEFAULT_CONFIG = """[reticulum]
  enable_transport = No
  share_instance = Yes
  instance_name = default
  discover_interfaces = Yes

[logging]
  loglevel = 4

[interfaces]
  [[Default Interface]]
    type = AutoInterface
    enabled = Yes
"""


def update_rns_config(config_dir, address, args):
    config_dir = Path(config_dir).expanduser()
    config_path = config_dir / "config"
    block_name = "RNode BLE Interface"
    port_line = f"port = ble://{address}"

    if config_path.exists():
        text = config_path.read_text()
        if port_line in text:
            print(f"'{config_path}' already has an interface for {address}, leaving it as-is.")
            return
    else:
        print(f"No config found at {config_path}, creating a default one first...")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_CONFIG)

    backup_path = config_path.with_name(config_path.name + ".bak." + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    shutil.copy2(config_path, backup_path)
    print(f"Backed up existing config to {backup_path}")

    block = (
        f"\n[[{block_name}]]\n"
        f"  type = RNodeInterface\n"
        f"  interface_enabled = True\n"
        f"  {port_line}\n"
        f"  frequency = {args.frequency}\n"
        f"  bandwidth = {args.bandwidth}\n"
        f"  txpower = {args.txpower}\n"
        f"  spreadingfactor = {args.spreadingfactor}\n"
        f"  codingrate = {args.codingrate}\n"
    )

    with config_path.open("a") as f:
        f.write(block)
    print(f"Added '[[{block_name}]]' to {config_path}")


def create_or_load_identity(identity_path):
    import RNS

    identity_path = Path(identity_path).expanduser()
    if identity_path.exists():
        identity = RNS.Identity.from_file(str(identity_path))
        print(f"Loaded existing identity from {identity_path}")
    else:
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity = RNS.Identity()
        identity.to_file(str(identity_path))
        print(f"Created new identity at {identity_path}")

    print(f"Identity hash: {RNS.prettyhexrep(identity.hash)}")
    return identity


def load_state(state_path):
    state_path = Path(state_path).expanduser()
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except ValueError:
        return None


def save_state(state_path, address):
    state_path = Path(state_path).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "address": address,
        "paired_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }, indent=2) + "\n")


def rns_tool_path(name):
    venv_bin = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(venv_bin):
        return venv_bin
    found = shutil.which(name)
    if found:
        return found
    print(f"Couldn't find '{name}'. Make sure the venv is active or 'rns' is installed (pip install rns).")
    sys.exit(1)


def launch_rnsd(config_path):
    rnsd_path = rns_tool_path("rnsd")
    print()
    print(f"Launching rnsd (config: {config_path})... Ctrl+C to stop.")
    os.execv(rnsd_path, [rnsd_path, "--config", str(Path(config_path).expanduser())])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="RNode USB serial port (auto-detected if omitted)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--pin-timeout", type=float, default=20.0, help="Seconds to wait for the RNode to report its pairing PIN")
    parser.add_argument("--skip-pair", action="store_true", help="Skip the USB pairing flow (RNode is already bonded)")
    parser.add_argument("--repair", action="store_true", help="Ignore any saved pairing and run the USB pairing flow again")
    parser.add_argument("--address", help="RNode BLE MAC address, e.g. aa:bb:cc:dd:ee:ff (skips auto-detection)")
    parser.add_argument("--state-file", default=str(Path(__file__).parent / "rnode_state.json"), help="Where to remember the paired RNode's address")
    parser.add_argument("--config", default=None, help="Path to the RNS config directory (skips the startup config prompt if given)")
    parser.add_argument("--identity", default=str(Path(__file__).parent / "identity"), help="Path to the RNS identity file to create/reuse")
    parser.add_argument("--frequency", type=int, default=915000000, help="LoRa frequency in Hz")
    parser.add_argument("--bandwidth", type=int, default=125000, help="LoRa bandwidth in Hz")
    parser.add_argument("--txpower", type=int, default=17, help="LoRa TX power in dBm")
    parser.add_argument("--spreadingfactor", type=int, default=8, help="LoRa spreading factor")
    parser.add_argument("--codingrate", type=int, default=5, help="LoRa coding rate")
    parser.add_argument("--no-run", action="store_true", help="Update config/identity but don't launch rnsd")
    args = parser.parse_args()
    args.config = resolve_config_dir(args.config)

    address = args.address
    if not address and not args.repair:
        state = load_state(args.state_file)
        if state and state.get("address"):
            address = state["address"]
            print(f"Using previously paired RNode {address} (paired {state.get('paired_at')}).")
            print("Pass --repair to pair a different device instead.")

    if not address and not args.skip_pair:
        address = pair_rnode(args)
    elif not address:
        address = find_bonded_rnode_address()

    if address:
        save_state(args.state_file, address)
        update_rns_config(args.config, address, args)
    else:
        print("No RNode found (not plugged in and none previously paired) -- continuing without adding a BLE interface.")

    create_or_load_identity(args.identity)

    if args.no_run:
        print("\n--no-run set, not launching rnsd.")
        return

    launch_rnsd(args.config)


if __name__ == "__main__":
    main()
