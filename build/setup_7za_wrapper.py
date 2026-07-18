"""Install the 7za wrapper into node_modules so electron-builder can build on
Windows without admin / Developer Mode.

Run this once after a fresh `npm install`:
    python build/setup_7za_wrapper.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TARGET = ROOT / "node_modules" / "7zip-bin" / "win" / "x64" / "7za.exe"
WRAPPER = HERE / "7za.exe"
REAL_BACKUP = TARGET.parent / "7za-real.exe"


def build_wrapper_if_needed() -> None:
    """Build the wrapper with PyInstaller if it's not already present."""
    if WRAPPER.exists():
        return
    print(f"[setup] building {WRAPPER} ...")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[setup] PyInstaller not found. Install with: pip install pyinstaller",
              file=sys.stderr)
        sys.exit(1)
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--name", "7za",
        "--distpath", str(HERE),
        "--workpath", str(HERE / "_build_work"),
        "--specpath", str(HERE / "_build_work"),
        "--log-level", "WARN",
        str(HERE / "_7za_wrapper.py"),
    ])


def install() -> None:
    if not TARGET.exists():
        print(f"[setup] target not found: {TARGET}. Run `npm install` first.",
              file=sys.stderr)
        sys.exit(1)
    build_wrapper_if_needed()

    # Back up the real 7za.exe once, then swap
    if not REAL_BACKUP.exists():
        shutil.copy2(TARGET, REAL_BACKUP)
        print(f"[setup] backed up original -> {REAL_BACKUP.name}")
    shutil.copy2(WRAPPER, TARGET)
    print(f"[setup] installed wrapper -> {TARGET}")
    print("[setup] done. You can now `npm run build`.")


if __name__ == "__main__":
    install()
