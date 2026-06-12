import sys

from pathlib import Path


def bundle_root() -> Path:
    """Root of the read-only resources PyInstaller packs into the binary (ffmpeg.exe,
    vs/ scripts). The repo root when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent


def app_root() -> Path:
    """Directory of user-facing files that live next to the executable and persist
    across runs (keys.json, Subtitle/, font/). The repo root when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent
