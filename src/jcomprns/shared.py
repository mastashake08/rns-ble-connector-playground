"""Small helpers shared between the interactive RNS apps in this package."""

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

_verbose = False


def set_verbose(enabled):
    global _verbose
    _verbose = enabled


def is_verbose():
    return _verbose


def debug(message):
    """Print a diagnostic message only when --verbose is enabled. Call this
    at points where an error is otherwise handled silently by design (a
    best-effort OS notification failing, a corrupt JSON file falling back
    to a default, garbage network data being ignored, ...) so the detail
    isn't lost when troubleshooting, but doesn't clutter normal runs."""
    if _verbose:
        print(f"[debug] {message}", file=sys.stderr)


def app_data_dir():
    """Where this app's runtime state lives: identity, remembered pairing
    state, presence directories, received files, and saved config profiles.
    Independent of where the package itself is installed (pip may put that
    anywhere, including a read-only location), same idea as RNS's own
    ~/.reticulum."""
    path = Path.home() / ".jcomprns"
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_IDENTITY = str(app_data_dir() / "identity")
DEFAULT_STATE_FILE = str(app_data_dir() / "rnode_state.json")
DEFAULT_CONTACTS = str(app_data_dir() / "contacts.json")
DEFAULT_MESSAGES = str(app_data_dir() / "messages.json")
DEFAULT_FILETRANSFER_CONTACTS = str(app_data_dir() / "filetransfer_contacts.json")
DEFAULT_RECEIVED_DIR = str(app_data_dir() / "received_files")
DEFAULT_MANIFEST = str(app_data_dir() / "received_files.json")
CONFIGS_DIR = app_data_dir() / "configs"


def applescript_escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title, subtitle, body):
    """Best-effort native OS notification. Never raises -- notifications are
    a nice-to-have, not something that should crash the app if the native
    tool is missing, times out, or the platform isn't one of the three
    handled below."""
    system = platform.system()
    try:
        if system == "Darwin":
            _notify_macos(title, subtitle, body)
        elif system == "Windows":
            _notify_windows(title, subtitle, body)
        elif system == "Linux":
            _notify_linux(title, subtitle, body)
        else:
            debug(f"notify(): no notifier for platform {system!r}")
    except (OSError, subprocess.SubprocessError) as e:
        debug(f"notify() failed: {e}")


def _notify_macos(title, subtitle, body):
    script = (
        f'display notification "{applescript_escape(body[:200])}" '
        f'with title "{applescript_escape(title)}" '
        f'subtitle "{applescript_escape(subtitle[:120])}"'
    )
    subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def _notify_linux(title, subtitle, body):
    if not shutil.which("notify-send"):
        debug("notify(): notify-send not found on PATH")
        return
    message = f"{subtitle}\n{body}" if subtitle else body
    subprocess.run(["notify-send", title, message], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def _notify_windows(title, subtitle, body):
    if not shutil.which("powershell"):
        debug("notify(): powershell not found on PATH")
        return
    message = f"{subtitle}\n{body}" if subtitle else body
    # Uses the WinRT toast APIs directly, so it works on stock Windows 10/11
    # with no extra modules (e.g. BurntToast) installed. Title/body are
    # passed as bound script parameters (not interpolated into the script
    # text) since they can contain untrusted network content.
    script = (
        "& { param([string]$Title,[string]$Body) "
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
        "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$textNodes = $template.GetElementsByTagName('text'); "
        "$textNodes.Item(0).AppendChild($template.CreateTextNode($Title)) | Out-Null; "
        "$textNodes.Item(1).AppendChild($template.CreateTextNode($Body)) | Out-Null; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PowerShell'); "
        "$notifier.Show($toast) "
        "}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script, "-Title", title, "-Body", message],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
    )


def load_json(path, default):
    path = Path(path).expanduser()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except ValueError as e:
        debug(f"load_json({path}) failed to parse, using default: {e}")
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
