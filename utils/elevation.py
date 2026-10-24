import ctypes
import subprocess
import sys

from pathlib import Path


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def relaunch_as_admin(extra: list[str]) -> bool:
    """Starts this command again elevated in its own console window, or returns False when the
    UAC prompt was declined. A frozen exe is its own sys.executable, while a run from source has
    to hand Python the script as well."""
    args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    params = subprocess.list2cmdline([*args, *extra])
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, str(Path.cwd()), 1
    )
    return result > 32
