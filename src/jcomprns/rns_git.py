#!/usr/bin/env python3
"""
Git over Reticulum.

Serves git repositories over Reticulum, and (via the companion
git-remote-jcomprns helper) lets you clone/fetch/push using a completely
normal git command line, no different from an ssh:// remote.

  Server:
    python3 rns_git.py serve --repos-dir /path/to/repos

  Client (once git-remote-jcomprns is on your PATH -- see README):
    git clone jcomprns://<hex-address>/<reponame>
    git remote add origin jcomprns://<hex-address>/<reponame>
    git fetch / git push   # just works

This works the same way ssh does for git: git already knows how to speak
its own wire protocol over an arbitrary bidirectional byte stream (that's
literally what happens over ssh -- `ssh host git-upload-pack '/repo'` pipes
git's pack protocol over the ssh channel). We provide that same stream over
an RNS Link using RNS.Buffer.create_bidirectional_buffer() (an RNS.Channel
wrapped in a real Python file-like object), and spawn the actual
git-upload-pack/git-receive-pack binaries on the serving side, piping their
stdin/stdout to the buffer. Git itself needs no changes; only the tiny
git-remote-jcomprns helper is needed on the client, to speak git's remote
helper protocol (see `git help remote-helpers`) and hand off to the same
connect/relay logic used here.
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import RNS

from .rnode_pair import create_or_load_identity, resolve_config_dir
from .shared import DEFAULT_IDENTITY, debug, set_verbose

APP_NAME = "jcomprns"
ASPECT = "git"
URL_SCHEME = "jcomprns"

LINK_TIMEOUT = 15.0
PATH_TIMEOUT = 15.0
GIT_SERVICES = ("upload-pack", "receive-pack")


def resolve_repo_path(repos_dir, reponame):
    """Map a requested repo name to a real directory under repos_dir,
    accepting the name with or without a trailing .git, and refusing
    anything that would resolve outside repos_dir."""
    repos_dir = Path(repos_dir).resolve()
    reponame = reponame.strip("/")
    if not reponame or ".." in Path(reponame).parts:
        return None

    stem = reponame[:-4] if reponame.endswith(".git") else reponame
    for name in (stem, stem + ".git"):
        candidate = (repos_dir / name).resolve()
        try:
            candidate.relative_to(repos_dir)
        except ValueError:
            continue
        if candidate.is_dir():
            return candidate
    return None


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class GitServerSession:
    """One incoming Link: reads a one-line request header
    ("<service> <reponame>\\n"), then spawns the matching git subprocess and
    relays bytes between it and the Reticulum buffer for the rest of the
    Link's lifetime."""

    def __init__(self, link, repos_dir):
        self.repos_dir = repos_dir
        self.proc = None
        self.header_buf = b""
        self.header_done = False
        self.lock = threading.Lock()
        channel = link.get_channel()
        self.buffer = RNS.Buffer.create_bidirectional_buffer(0, 0, channel, self._on_ready)

    def _on_ready(self, ready_bytes):
        data = self.buffer.read(ready_bytes)
        if not data:
            return
        with self.lock:
            if not self.header_done:
                self.header_buf += data
                if b"\n" not in self.header_buf:
                    return
                header, data = self.header_buf.split(b"\n", 1)
                self.header_done = True
                self._start_process(header.decode("utf-8", "replace"))

            if self.proc and self.proc.stdin and not self.proc.stdin.closed and data:
                try:
                    self.proc.stdin.write(data)
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError) as e:
                    debug(f"GitServerSession: write to git subprocess stdin failed: {e}")

    def _start_process(self, header_line):
        parts = header_line.strip().split(" ", 1)
        if len(parts) != 2 or parts[0] not in GIT_SERVICES:
            RNS.log(f"Rejecting git session: bad request {header_line!r}", RNS.LOG_ERROR)
            self._fail()
            return

        service, reponame = parts
        repo_path = resolve_repo_path(self.repos_dir, reponame)
        if not repo_path:
            RNS.log(f"Rejecting git session: unknown repo {reponame!r}", RNS.LOG_ERROR)
            self._fail()
            return

        RNS.log(f"Serving git-{service} for {repo_path}")
        self.proc = subprocess.Popen(
            [f"git-{service}", str(repo_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()

    def _fail(self):
        try:
            self.buffer.close()
        except Exception as e:
            debug(f"GitServerSession: closing buffer after failure raised: {e}")

    def _pump_stdout(self):
        try:
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                self.buffer.write(chunk)
                self.buffer.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            debug(f"GitServerSession: pumping git subprocess stdout failed: {e}")
        finally:
            returncode = self.proc.wait()
            RNS.log(f"git process exited with status {returncode}")
            self._fail()

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class GitServer:
    def __init__(self, config_dir, identity_path, repos_dir, announce_interval=0, verbose=False):
        self.reticulum = RNS.Reticulum(str(Path(config_dir).expanduser()), loglevel=RNS.LOG_DEBUG if verbose else None)
        self.identity = create_or_load_identity(identity_path)
        self.repos_dir = Path(repos_dir).expanduser().resolve()

        self.destination = RNS.Destination(
            self.identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT
        )
        self.destination.set_link_established_callback(self._on_link)

        self.announce_interval = announce_interval
        if announce_interval > 0:
            threading.Thread(target=self._announce_loop, daemon=True).start()
        self.announce_self()

    @property
    def address(self):
        return self.destination.hash.hex()

    def announce_self(self):
        self.destination.announce()

    def _announce_loop(self):
        while True:
            time.sleep(self.announce_interval * 60)
            self.announce_self()

    def _on_link(self, link):
        RNS.log("Incoming git connection")
        session = GitServerSession(link, self.repos_dir)
        link.set_link_closed_callback(lambda l: session.close())


def serve(args):
    config_dir = resolve_config_dir(args.config)
    repos_dir = Path(args.repos_dir).expanduser()
    if not repos_dir.is_dir():
        print(f"No such directory: {repos_dir}")
        sys.exit(1)

    server = GitServer(config_dir, args.identity, repos_dir, announce_interval=args.announce_interval, verbose=args.verbose)
    print(f"Serving git repositories from {repos_dir}")
    print(f"Your jcomprns git address: {server.address}")
    print(f"Share this with clients as: git clone {URL_SCHEME}://{server.address}/<reponame>")
    print("Press Enter to announce again, Ctrl+C to quit.")
    try:
        while True:
            input()
            server.announce_self()
            print("Announced.")
    except (KeyboardInterrupt, EOFError):
        print()


# --------------------------------------------------------------------------
# Client (used by the git-remote-jcomprns helper)
# --------------------------------------------------------------------------

def parse_url(url):
    prefix = f"{URL_SCHEME}://"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix):]
    if "/" not in rest:
        return None
    address_hex, reponame = rest.split("/", 1)
    return address_hex, reponame


def _establish(config_dir, identity_path, address_hex, verbose=False):
    """Bring up Reticulum and establish a link to a jcomprns git server.
    Returns the link on success, or None on failure (with a message
    already written to stderr)."""
    RNS.Reticulum(str(Path(config_dir).expanduser()), loglevel=RNS.LOG_DEBUG if verbose else None)
    create_or_load_identity(identity_path)

    try:
        dest_hash = bytes.fromhex(address_hex)
    except ValueError:
        sys.stderr.write(f"jcomprns: invalid address {address_hex!r}\n")
        return None

    if not RNS.Transport.has_path(dest_hash):
        sys.stderr.write("jcomprns: requesting path to git server...\n")
        RNS.Transport.request_path(dest_hash)
        deadline = time.time() + PATH_TIMEOUT
        while not RNS.Transport.has_path(dest_hash) and time.time() < deadline:
            time.sleep(0.2)
        if not RNS.Transport.has_path(dest_hash):
            sys.stderr.write("jcomprns: could not find a path to the git server\n")
            return None

    recipient_identity = RNS.Identity.recall(dest_hash)
    if not recipient_identity:
        sys.stderr.write("jcomprns: could not resolve the git server's identity\n")
        return None

    dest = RNS.Destination(recipient_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME, ASPECT)

    established = threading.Event()
    link = RNS.Link(dest, established_callback=lambda l: established.set())
    if not established.wait(timeout=LINK_TIMEOUT):
        sys.stderr.write("jcomprns: could not establish a link to the git server\n")
        link.teardown()
        return None

    return link


def relay(link, service, reponame, stdin_raw, stdout_raw):
    """Given an established link, send the request header, then
    transparently pump bytes between our own process stdio and the remote
    until either side is done. This is what makes us a valid transport for
    git's "connect" remote-helper capability."""
    remote_eof = threading.Event()

    def on_ready(n):
        data = buffer.read(n)
        if data:
            stdout_raw.write(data)
            stdout_raw.flush()
        else:
            remote_eof.set()

    channel = link.get_channel()
    buffer = RNS.Buffer.create_bidirectional_buffer(0, 0, channel, on_ready)
    buffer.write(f"{service} {reponame}\n".encode("utf-8"))
    buffer.flush()

    stdin_done = threading.Event()

    def pump_stdin():
        try:
            while True:
                chunk = stdin_raw.read1(65536)
                if not chunk:
                    break
                buffer.write(chunk)
                buffer.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            debug(f"relay(): pumping stdin to buffer failed: {e}")
        finally:
            try:
                buffer.close()
            except Exception as e:
                debug(f"relay(): closing buffer failed: {e}")
            stdin_done.set()

    threading.Thread(target=pump_stdin, daemon=True).start()

    link_closed = threading.Event()
    link.set_link_closed_callback(lambda l: link_closed.set())

    while not link_closed.is_set() and not (stdin_done.is_set() and remote_eof.is_set()):
        time.sleep(0.1)

    link.teardown()


def remote_helper_main():
    """Entry point for the git-remote-jcomprns shim. Speaks the minimal
    subset of git's remote-helper protocol needed for the "connect"
    capability -- see `git help remote-helpers`."""
    if len(sys.argv) < 3:
        sys.stderr.write("usage: git-remote-jcomprns <remote> <url>\n")
        sys.exit(1)

    parsed = parse_url(sys.argv[2])
    if not parsed:
        sys.stderr.write(f"jcomprns: unsupported URL {sys.argv[2]!r}, expected {URL_SCHEME}://<address>/<repo>\n")
        sys.exit(1)
    address_hex, reponame = parsed

    config_dir = os.environ.get("JCOMPRNS_CONFIG", "~/.reticulum")
    identity_path = os.environ.get("JCOMPRNS_IDENTITY", DEFAULT_IDENTITY)
    verbose = os.environ.get("JCOMPRNS_VERBOSE", "") not in ("", "0")
    set_verbose(verbose)

    stdin_raw = sys.stdin.buffer
    stdout_raw = sys.stdout.buffer

    while True:
        line = stdin_raw.readline()
        if not line:
            return
        command = line.decode("utf-8", "replace").strip("\n")

        if command == "capabilities":
            stdout_raw.write(b"connect\n\n")
            stdout_raw.flush()
        elif command.startswith("connect "):
            service = command[len("connect "):].strip()
            link = _establish(config_dir, identity_path, address_hex, verbose=verbose)
            if not link:
                return  # No ack sent -- git will report the helper failed to connect.
            stdout_raw.write(b"\n")
            stdout_raw.flush()
            relay(link, service, reponame, stdin_raw, stdout_raw)
            return
        elif command == "":
            return
        else:
            stdout_raw.write(b"\n")
            stdout_raw.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    serve_parser = subparsers.add_parser("serve", help="Serve git repositories over Reticulum")
    serve_parser.add_argument("-v", "--verbose", action="store_true",
                               help="Show diagnostic detail for errors that are normally handled silently, and run RNS at debug log level")
    serve_parser.add_argument("--repos-dir", required=True, help="Directory containing bare repositories to serve")
    serve_parser.add_argument("--config", default=None, help="Path to the RNS config directory (skips the startup config prompt if given)")
    serve_parser.add_argument("--identity", default=DEFAULT_IDENTITY, help="Path to the RNS identity file to create/reuse")
    serve_parser.add_argument("--announce-interval", type=float, default=0,
                               help="Re-announce yourself every N minutes so clients can discover you (0 = only announce once at startup)")

    args = parser.parse_args()
    if args.mode == "serve":
        set_verbose(args.verbose)
        serve(args)


if __name__ == "__main__":
    main()
