#!/usr/bin/env python3
"""
Interactive LXMF messaging client over the Reticulum network set up by
rnode_pair.py.

Runs Reticulum in-process (attaching to the shared instance if rnsd is
already running, or bringing up the configured interfaces itself if not),
registers an LXMF delivery destination for your identity, and gives you a
tiny keyboard-driven UI:

  [M] Compose a message to a pasted LXMF address
  [I] Open the inbox and optionally reply to a message
  [Q] Quit

Incoming messages trigger a terminal alert and a native macOS notification.
"""

import argparse
import queue
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

import RNS
import LXMF

from rnode_pair import create_or_load_identity, resolve_config_dir

DEFAULT_IDENTITY = str(Path(__file__).parent / "identity")


def applescript_escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify_macos(title, subtitle, body):
    try:
        script = (
            f'display notification "{applescript_escape(body[:200])}" '
            f'with title "{applescript_escape(title)}" '
            f'subtitle "{applescript_escape(subtitle[:120])}"'
        )
        subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class Messenger:
    def __init__(self, config_dir, identity_path, display_name, stamp_cost):
        self.reticulum = RNS.Reticulum(str(Path(config_dir).expanduser()))
        self.identity = create_or_load_identity(identity_path)

        storage_dir = Path(config_dir).expanduser() / "lxmf"
        self.router = LXMF.LXMRouter(storagepath=str(storage_dir), enforce_stamps=False)
        self.router.register_delivery_callback(self._on_message)
        self.destination = self.router.register_delivery_identity(
            self.identity, display_name=display_name, stamp_cost=stamp_cost
        )

        self.inbox = []
        self.inbox_lock = threading.Lock()
        self.notify_queue = queue.Queue()

        self.router.announce(self.destination.hash)

    @property
    def address(self):
        return self.destination.hash.hex()

    def _on_message(self, message):
        with self.inbox_lock:
            self.inbox.append(message)
        self.notify_queue.put(message)

    def drain_notifications(self):
        while True:
            try:
                message = self.notify_queue.get_nowait()
            except queue.Empty:
                return
            preview = message.content_as_string() or ""
            print(f"\n\a\U0001F4E9 New message from {message.source_hash.hex()}: {preview[:80]}")
            print("Press [I] for inbox, [M] to compose, [Q] to quit.")
            notify_macos("LXMF Message", f"From {message.source_hash.hex()[:16]}...", preview)

    def send(self, address_hex, title, body):
        try:
            dest_hash = bytes.fromhex(address_hex.strip())
        except ValueError:
            print("That doesn't look like a valid hex address.")
            return

        if not RNS.Transport.has_path(dest_hash):
            print("Path to recipient unknown, requesting...")
            RNS.Transport.request_path(dest_hash)
            deadline = time.time() + 15
            while not RNS.Transport.has_path(dest_hash) and time.time() < deadline:
                time.sleep(0.2)
            if not RNS.Transport.has_path(dest_hash):
                print("Could not find a path to that address. They may be offline or out of range.")
                return

        recipient_identity = RNS.Identity.recall(dest_hash)
        if not recipient_identity:
            print("Could not resolve an identity for that address.")
            return

        dest = RNS.Destination(recipient_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
        self._deliver(dest, title, body)
        print("Message sent.")

    def reply(self, message, body):
        self._deliver(message.source, "", body)
        print("Reply sent.")

    def _deliver(self, dest, title, body):
        lxm = LXMF.LXMessage(dest, self.destination, body, title, desired_method=LXMF.LXMessage.DIRECT, include_ticket=True)
        self.router.handle_outbound(lxm)


def compose(messenger):
    print()
    address_hex = input("To (LXMF address hex, blank to cancel): ").strip()
    if not address_hex:
        print("Cancelled.")
        return
    title = input("Title (optional): ").strip()
    body = input("Message: ")
    if not body:
        print("Cancelled (empty message).")
        return
    messenger.send(address_hex, title, body)


def show_inbox(messenger):
    with messenger.inbox_lock:
        messages = list(messenger.inbox)

    print("\n--- Inbox ---")
    if not messages:
        print("(empty)")
        return

    for i, m in enumerate(messages):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.timestamp))
        title = m.title_as_string() or "(no title)"
        print(f"[{i}] {ts}  from {m.source_hash.hex()}")
        print(f"     {title}: {m.content_as_string()}")

    choice = input("Enter number to reply, or blank to go back: ").strip()
    if choice.isdigit() and int(choice) in range(len(messages)):
        body = input("Reply: ")
        if body:
            messenger.reply(messages[int(choice)], body)
        else:
            print("Cancelled.")


def run_keyboard_loop(messenger):
    print(f"Your LXMF address: {messenger.address}")
    print("[M] Compose   [I] Inbox   [Q] Quit\n")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if ready:
                ch = sys.stdin.read(1).lower()
                if ch == "m":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    compose(messenger)
                    tty.setcbreak(fd)
                elif ch == "i":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    show_inbox(messenger)
                    tty.setcbreak(fd)
                elif ch == "q":
                    break
            messenger.drain_notifications()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None, help="Path to the RNS config directory (skips the startup config prompt if given)")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY, help="Path to the RNS identity file to create/reuse")
    parser.add_argument("--display-name", default="ble-connector", help="Display name announced with your LXMF address")
    parser.add_argument("--stamp-cost", type=int, default=0, help="Proof-of-work stamp cost required from senders")
    args = parser.parse_args()
    args.config = resolve_config_dir(args.config)

    messenger = Messenger(args.config, args.identity, args.display_name, args.stamp_cost)

    try:
        run_keyboard_loop(messenger)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
