#!/usr/bin/env python3
"""
Interactive file transfer client over the Reticulum network set up by
rnode_pair.py.

Runs Reticulum in-process (attaching to the shared instance if rnsd is
already running, or bringing up the configured interfaces itself if not),
registers a "jcomprns.filetransfer" destination for your identity, and
gives you a tiny keyboard-driven UI:

  [S] Send a file to a pasted address
  [R] List received files and where they were saved
  [P] Open the presence directory of peers seen announcing on the network
  [Q] Quit

Files are sent over an RNS Link using RNS.Resource, which handles chunking,
compression and integrity checking for you. This is a different destination
namespace from lxmf_messenger.py ("jcomprns.filetransfer" vs
"lxmf.delivery"), so it has its own address, contacts, and announces --
even though it can share the same identity file.

Note (from RNS's own docs): Resources aren't recommended for very large
files, since compression/encryption/hashmap sequencing can take longer than
the receiver's timeout on slow links or slow CPUs. Fine for the kind of
files you'd send over a LoRa-connected RNode; if you need to move large
files routinely, look at RNS's Bundle class instead.
"""

import argparse
import queue
import select
import sys
import termios
import threading
import time
import tty
from pathlib import Path

import RNS
import RNS.vendor.umsgpack as msgpack

from rnode_pair import create_or_load_identity, resolve_config_dir
from shared import notify, load_json, save_json, human_size

DEFAULT_IDENTITY = str(Path(__file__).parent / "identity")
DEFAULT_CONTACTS = str(Path(__file__).parent / "filetransfer_contacts.json")
DEFAULT_RECEIVED_DIR = str(Path(__file__).parent / "received_files")
DEFAULT_MANIFEST = str(Path(__file__).parent / "received_files.json")

APP_NAME = "jcomprns"
ASPECT = "filetransfer"
ASPECT_FILTER = f"{APP_NAME}.{ASPECT}"

LINK_TIMEOUT = 15.0
PATH_TIMEOUT = 15.0


def decode_peer_name(app_data):
    """Our own announce app_data is msgpack([display_name_bytes_or_None]).
    app_data comes from the network, so any shape of garbage is expected."""
    if not app_data:
        return None
    try:
        unpacked = msgpack.unpackb(app_data)
        name_bytes = unpacked[0] if isinstance(unpacked, list) and unpacked else None
        return name_bytes.decode("utf-8") if name_bytes else None
    except Exception:
        return None


class FileTransferNode:
    def __init__(self, config_dir, identity_path, display_name,
                 received_dir=DEFAULT_RECEIVED_DIR, manifest_path=DEFAULT_MANIFEST,
                 contacts_path=DEFAULT_CONTACTS, announce_interval=0):
        self.reticulum = RNS.Reticulum(str(Path(config_dir).expanduser()))
        self.identity = create_or_load_identity(identity_path)
        self.display_name = display_name

        self.destination = RNS.Destination(
            self.identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT
        )
        self.destination.set_link_established_callback(self._on_incoming_link)

        self.received_dir = Path(received_dir).expanduser()
        self.received_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = Path(manifest_path).expanduser()
        self.manifest_lock = threading.Lock()
        self.manifest = load_json(self.manifest_path, [])

        self.notify_queue = queue.Queue()

        # Presence directory, same mechanism as lxmf_messenger.py but scoped
        # to this app's own destination namespace.
        self.aspect_filter = ASPECT_FILTER
        self.contacts_path = Path(contacts_path).expanduser()
        self.contacts = load_json(self.contacts_path, {})
        self.contacts_lock = threading.Lock()
        self.presence_queue = queue.Queue()
        RNS.Transport.register_announce_handler(self)

        self.announce_interval = announce_interval
        if announce_interval > 0:
            threading.Thread(target=self._announce_loop, daemon=True).start()

        self.announce_self()

    @property
    def address(self):
        return self.destination.hash.hex()

    def announce_self(self):
        name_bytes = self.display_name.encode("utf-8") if self.display_name else None
        self.destination.announce(app_data=msgpack.packb([name_bytes]))

    def _announce_loop(self):
        while True:
            time.sleep(self.announce_interval * 60)
            self.announce_self()

    def received_announce(self, destination_hash, announced_identity, app_data):
        if destination_hash == self.destination.hash:
            return

        address = destination_hash.hex()
        name = decode_peer_name(app_data)
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        with self.contacts_lock:
            entry = self.contacts.get(address, {})
            is_new = address not in self.contacts
            entry["name"] = name or entry.get("name")
            entry["last_seen"] = now
            entry.setdefault("first_seen", now)
            self.contacts[address] = entry
            save_json(self.contacts_path, self.contacts)

        self.presence_queue.put((address, entry["name"], is_new))

    # --- receiving -----------------------------------------------------

    def _on_incoming_link(self, link):
        link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
        link.set_resource_started_callback(self._on_resource_started)
        link.set_resource_concluded_callback(self._on_resource_concluded)
        self.notify_queue.put(("status", "Incoming connection, standing by for a file..."))

    def _on_resource_started(self, resource):
        self.notify_queue.put(("status", f"Receiving a file ({human_size(resource.total_size)})..."))

    def _on_resource_concluded(self, resource):
        if resource.status != RNS.Resource.COMPLETE:
            self.notify_queue.put(("failed", "An incoming file transfer failed."))
            return

        meta = resource.metadata or {}
        filename = meta.get("filename") or f"file_{int(time.time())}.bin"
        data = resource.data.read()

        dest_path = self._unique_path(filename)
        dest_path.write_bytes(data)

        entry = {
            "filename": dest_path.name,
            "size": len(data),
            "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "path": str(dest_path),
        }
        with self.manifest_lock:
            self.manifest.append(entry)
            save_json(self.manifest_path, self.manifest)

        self.notify_queue.put(("received", entry))

    def _unique_path(self, filename):
        stem, suffix = Path(filename).stem, Path(filename).suffix
        candidate = self.received_dir / filename
        counter = 1
        while candidate.exists():
            candidate = self.received_dir / f"{stem}.{counter}{suffix}"
            counter += 1
        return candidate

    def drain_notifications(self):
        while True:
            try:
                kind, payload = self.notify_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "received":
                print(f"\n\a\U0001F4E6 File received: {payload['filename']} ({human_size(payload['size'])})")
                print("Press [R] to view received files.")
                notify("File Received", payload["filename"], human_size(payload["size"]))
            else:
                print(f"\n{payload}")

        while True:
            try:
                address, name, is_new = self.presence_queue.get_nowait()
            except queue.Empty:
                return
            if is_new:
                label = f"{name} ({address})" if name else address
                print(f"\n\U0001F7E2 New file-transfer peer seen: {label}")
                print("Press [P] for the presence directory.")

    # --- sending ---------------------------------------------------------

    def send_file(self, address_hex, path):
        path = Path(path).expanduser()
        if not path.is_file():
            print(f"No such file: {path}")
            return

        try:
            dest_hash = bytes.fromhex(address_hex.strip())
        except ValueError:
            print("That doesn't look like a valid hex address.")
            return

        if not RNS.Transport.has_path(dest_hash):
            print("Path to recipient unknown, requesting...")
            RNS.Transport.request_path(dest_hash)
            deadline = time.time() + PATH_TIMEOUT
            while not RNS.Transport.has_path(dest_hash) and time.time() < deadline:
                time.sleep(0.2)
            if not RNS.Transport.has_path(dest_hash):
                print("Could not find a path to that address. They may be offline or out of range.")
                return

        recipient_identity = RNS.Identity.recall(dest_hash)
        if not recipient_identity:
            print("Could not resolve an identity for that address.")
            return

        dest = RNS.Destination(recipient_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME, ASPECT)

        established = threading.Event()
        link = RNS.Link(dest, established_callback=lambda l: established.set())

        print("Establishing link with recipient...")
        if not established.wait(timeout=LINK_TIMEOUT):
            print("Could not establish a link (they may be offline). Transfer cancelled.")
            link.teardown()
            return

        filesize = path.stat().st_size
        print(f"Sending {path.name} ({human_size(filesize)})...")

        done = threading.Event()
        result = {}

        def on_concluded(resource):
            result["status"] = resource.status
            done.set()

        file_handle = path.open("rb")
        resource = RNS.Resource(
            file_handle, link,
            metadata={"filename": path.name},
            callback=on_concluded,
        )

        while not done.is_set():
            percent = round(resource.get_progress() * 100, 1)
            print(f"\rProgress: {percent}%   ", end="", flush=True)
            done.wait(timeout=0.25)

        file_handle.close()
        print()
        if result.get("status") == RNS.Resource.COMPLETE:
            print("Transfer complete.")
        else:
            print("Transfer failed.")
        link.teardown()


def send_prompt(node, to_address=None, to_name=None):
    print()
    if to_address:
        print(f"To: {to_name + ' ' if to_name else ''}({to_address})")
        address_hex = to_address
    else:
        address_hex = input("To (address hex, blank to cancel): ").strip()
        if not address_hex:
            print("Cancelled.")
            return
    path = input("File path (blank to cancel): ").strip()
    if not path:
        print("Cancelled.")
        return
    node.send_file(address_hex, path)


def show_received(node):
    with node.manifest_lock:
        entries = list(node.manifest)

    print("\n--- Received Files ---")
    if not entries:
        print("(none yet)")
        return
    for i, e in enumerate(entries):
        print(f"[{i}] {e['received_at']}  {e['filename']}  ({human_size(e['size'])})")
        print(f"     saved to {e['path']}")


def show_presence(node):
    with node.contacts_lock:
        contacts = list(node.contacts.items())
    contacts.sort(key=lambda kv: kv[1].get("last_seen", ""), reverse=True)

    print("\n--- Presence Directory (file transfer) ---")
    print(f"Your address: {node.address}")
    if not contacts:
        print("(no peers seen yet -- they'll show up here once they announce on the network)")
    else:
        for i, (address, info) in enumerate(contacts):
            name = info.get("name") or "(no name)"
            print(f"[{i}] {name}  {address}")
            print(f"     first seen {info.get('first_seen')}  last seen {info.get('last_seen')}")

    choice = input("Enter number to send a file to someone, [A] to announce yourself, or blank to go back: ").strip()
    if choice.lower() == "a":
        node.announce_self()
        print("Announced.")
    elif choice.isdigit() and int(choice) in range(len(contacts)):
        address, info = contacts[int(choice)]
        send_prompt(node, to_address=address, to_name=info.get("name"))


def run_keyboard_loop(node):
    print(f"Your file-transfer address: {node.address}")
    print("[S] Send   [R] Received files   [P] Presence   [Q] Quit\n")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if ready:
                ch = sys.stdin.read(1).lower()
                if ch == "s":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    send_prompt(node)
                    tty.setcbreak(fd)
                elif ch == "r":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    show_received(node)
                    tty.setcbreak(fd)
                elif ch == "p":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    show_presence(node)
                    tty.setcbreak(fd)
                elif ch == "q":
                    break
            node.drain_notifications()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None, help="Path to the RNS config directory (skips the startup config prompt if given)")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY, help="Path to the RNS identity file to create/reuse")
    parser.add_argument("--display-name", default="ble-connector", help="Display name announced with your file-transfer address")
    parser.add_argument("--received-dir", default=DEFAULT_RECEIVED_DIR, help="Where incoming files are saved")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, help="Where the list of received files is recorded")
    parser.add_argument("--contacts", default=DEFAULT_CONTACTS, help="Where the presence directory is persisted")
    parser.add_argument("--announce-interval", type=float, default=0,
                         help="Re-announce yourself every N minutes so others can discover you (0 = only announce once at startup)")
    args = parser.parse_args()
    args.config = resolve_config_dir(args.config)

    node = FileTransferNode(args.config, args.identity, args.display_name,
                             received_dir=args.received_dir, manifest_path=args.manifest,
                             contacts_path=args.contacts, announce_interval=args.announce_interval)

    try:
        run_keyboard_loop(node)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
