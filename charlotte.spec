from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("vs", "vs"),
        *collect_data_files("vapoursynth"),
    ],
    hiddenimports=[
        "vsdeband",
        "vsdenoise",
        "vsjetpack",
        "vssource",
        "vstools",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["utils/runtime_hook.py"],
    excludes=["vspreview"],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

# Remove the duplicate libvapoursynth.dll from the root
a.binaries = [x for x in a.binaries if not x[0].lower().startswith('libvapoursynth.dll')]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="charlotte",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="docs/icon/icon.ico",
)
