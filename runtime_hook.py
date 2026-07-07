import ctypes
import os
import sys

from pathlib import Path


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    """
    PyInstaller incorrectly places libvapoursynth.dll at _MEIPASS/ as a detected dependency of
    vapoursynth.pyd. VS's C core uses GetModuleFileName on itself and resolves the plugin
    directory to _MEIPASS/plugins/ (nonexistent). The correct copy is at
    _MEIPASS/vapoursynth/libvapoursynth.dll. Pre-loading it by full path ensures subsequent
    LoadLibrary("libvapoursynth.dll") calls reuse this instance, giving the correct
    plugin path _MEIPASS/vapoursynth/plugins/.
    """
    meipass = Path(sys._MEIPASS)

    dll = meipass / "vapoursynth" / "libvapoursynth.dll"
    if dll.is_file():
        ctypes.WinDLL(str(dll))

    os.environ["PATH"] = f"{meipass}{os.pathsep}{os.environ.get('PATH', '')}"
