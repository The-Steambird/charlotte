import ctypes
import os
import sys

from pathlib import Path


if getattr(sys, "frozen", False):
    # The VapourSynth core finds its plugins next to its own DLL, and a copy that PyInstaller
    # puts at the bundle root would look in a plugins/ folder that does not exist there. Loading
    # the one in vapoursynth/ first makes every later LoadLibrary("libvapoursynth.dll") reuse it.
    meipass = Path(sys._MEIPASS)
    ctypes.WinDLL(str(meipass / "vapoursynth" / "libvapoursynth.dll"))

    os.environ["PATH"] = f"{meipass}{os.pathsep}{os.environ.get('PATH', '')}"
