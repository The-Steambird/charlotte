import ctypes
import os
import sys


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    """
    PyInstaller incorrectly places libvapoursynth.dll at _MEIPASS/ as a detected dependency of
    vapoursynth.pyd. VS's C core uses GetModuleFileName on itself and resolves the plugin
    directory to _MEIPASS/plugins/ (nonexistent). The correct copy is at
    _MEIPASS/vapoursynth/libvapoursynth.dll. Pre-loading it by full path ensures subsequent
    LoadLibrary("libvapoursynth.dll") calls reuse this instance, giving the correct
    plugin path _MEIPASS/vapoursynth/plugins/.
    """
    dll = os.path.join(sys._MEIPASS, "vapoursynth", "libvapoursynth.dll")
    if os.path.isfile(dll):
        ctypes.WinDLL(dll)
