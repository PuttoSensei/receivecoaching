# -*- mode: python ; coding: utf-8 -*-
# Bundles the FastAPI backend (server.py + receive_coach.py) into a
# self-contained dist-py/server/ folder so the packaged desktop app does not
# require Python on the host machine. Electron spawns dist-py/server/server.exe
# (shipped as resources/app/pyserver/) instead of `python server.py`.
#
# Build:
#   pyinstaller build/server_backend.spec --distpath dist-py --workpath build/_pyi --noconfirm
#
# Kept as console=True so stdout/stderr pipe into Electron's log capture;
# the console window itself is suppressed by windowsHide in the spawn call.

from PyInstaller.utils.hooks import collect_submodules

# uvicorn and anyio pick protocol/backend modules dynamically at runtime —
# static analysis misses them without explicit collection.
hiddenimports = collect_submodules('uvicorn') + collect_submodules('anyio')

# Optional / dynamically-probed packages: include whichever are installed.
# - pypdf: optional PDF extraction (receive_coach imports it in a try block)
# - multipart: fastapi's form/upload parsing imports it lazily
# - websockets / wsproto: uvicorn's ws protocol auto-detection
for pkg in ('pypdf', 'multipart', 'websockets', 'wsproto'):
    try:
        __import__(pkg)
        hiddenimports += collect_submodules(pkg)
    except ImportError:
        pass

a = Analysis(
    ['../server.py'],
    pathex=['..'],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pytest'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='server',
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='server',
)
