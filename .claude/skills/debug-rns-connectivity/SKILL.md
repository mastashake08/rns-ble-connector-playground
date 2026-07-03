---
name: debug-rns-connectivity
description: Diagnose rnsd/RNS interface errors and warnings in this project's logs — e.g. "module 'select' has no attribute 'epoll'", "No route to host", or "division by zero" / "announce queue has been cleared". Use whenever the user pastes rnsd log output or asks why an interface won't connect.
---

# Diagnosing rnsd interface errors

These are the known error patterns already seen in this project's `rnsd` output, and what they actually mean.

## `module 'select' has no attribute 'epoll'`

Cause: the interface is configured with `type = BackboneInterface`, which uses Linux/Android-only `epoll` I/O and simply cannot run on macOS. This is a hard platform limitation, not a config typo.

Fix: change that interface block to `type = TCPClientInterface` and rename its `remote =` key to `target_host =` (keep `target_port` as-is) — same job, works cross-platform:

```
# before (fails on macOS)
[[Some Hub]]
  type = BackboneInterface
  enabled = yes
  remote = example.com
  target_port = 4242

# after
[[Some Hub]]
  type = TCPClientInterface
  enabled = yes
  target_host = example.com
  target_port = 4242
```

Back up the config file before editing it (timestamped copy), same as `rnode_pair.py`'s own config-writing does.

## `No route to host`

If the target is an IPv6 address in the `200::/7` range, it's a Yggdrasil overlay-network address — your Mac has no route to it unless Yggdrasil is installed and actually running/connected locally. This is expected/normal, not a bug; nothing to change in the RNS config itself.

For other hosts, it's an ordinary network reachability problem (DNS, firewall, the remote being down) — treat it like any other unreachable-host case, not an RNS-specific issue.

## `Error while processing announce queue on RNodeInterface[...]. The contained exception was: division by zero` / `The announce queue for this interface has been cleared.`

Cause: `RNodeInterface.bitrate` starts at `0` and is only set once the RNode firmware confirms its radio parameters back over serial/BLE (`updateBitrate()`). RNS's base `Interface.process_announce_queue()` divides by `self.bitrate` to pace transmissions; if an announce needs relaying through that interface before the first radio-config confirmation lands, it's a division by zero. RNS catches it generically and clears that interface's announce queue.

This is cosmetic and non-fatal — `rnsd` keeps running and the interface keeps working. The only effect is that whatever announce(s) were queued for rebroadcast through that interface at that exact moment get dropped and wait for the next announce cycle. Typically only fires in the first second or two after the interface comes up. Already investigated against the latest available `rns` release (no fix upstream at time of writing) and intentionally left unpatched — don't attempt to "fix" it by changing radio parameters, retrying pairing, or similar; it isn't caused by anything in this project's config.

## General approach for anything not listed above

1. Check whether the error originates from RNS/LXMF library code (`.venv/lib/python*/site-packages/RNS/` or `.../LXMF/`) vs. this project's own scripts — `grep` the exact error string in the installed package first.
2. Check `python3 -m pip index versions rns` / `lxmf` against the installed version before assuming a bug needs a workaround here — it may already be fixed upstream.
3. Prefer fixing config (this project's files) over patching installed library internals; only consider a monkeypatch as a last resort, and call out explicitly that it's patching third-party internals so it's understood as fragile.
