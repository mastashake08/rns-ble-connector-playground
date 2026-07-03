---
name: add-config-profile
description: Save, list, or switch between Reticulum config profiles under configs/. Use when the user wants to add a new RNS config, save the current config as a named profile, or asks how the config-picker prompt at startup works.
---

# Config profiles

Both `rnode_pair.py` and `lxmf_messenger.py` call the same `resolve_config_dir()` (defined in `rnode_pair.py`) at startup. If `--config` isn't passed explicitly, it prompts:

```
Which Reticulum config do you want to use?
  [0] Your live config (~/.reticulum)
  [1] default  (configs/default)
Choice [0]:
```

- **[0]** / Enter — your live `~/.reticulum` config, same as running the tools always used to.
- Any other number — that profile directory under `configs/<name>/` is used as a fully isolated RNS config dir for this run: its own `config` file, its own `storage/`/interface state, completely separate from your live setup and from other profiles.

If there are no saved profiles under `configs/`, the prompt is skipped entirely and it silently uses the live config — so this only appears once at least one profile exists.

## Adding a new profile

```
mkdir -p configs/<name>
cp ~/.reticulum/config configs/<name>/config
```

That's it — it shows up in the picker automatically (`list_saved_configs()` just looks for subdirectories of `configs/` containing a `config` file). Only the `config` file itself is meant to be copied/hand-edited; don't copy over `storage/` or `interfaces/` from a live setup — those are runtime caches (identity/path caches etc.) that get created fresh the first time a profile is used, and copying them in would carry over cached state you probably don't want tied to a different profile.

## Skipping the prompt

Pass `--config <dir>` on the command line (works on both scripts) to use that directory directly without any prompt — useful for scripting or automated runs.
