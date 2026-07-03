"""Small helpers shared between the interactive RNS apps in this repo."""

import json
import subprocess
from pathlib import Path


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


def load_json(path, default):
    path = Path(path).expanduser()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except ValueError:
        return default


def save_json(path, data):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def human_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:3.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"
