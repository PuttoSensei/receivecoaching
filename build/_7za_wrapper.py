"""Windows 7-Zip wrapper that tolerates benign "can't create symlink" errors.

electron-builder's app-builder.exe extracts the winCodeSign archive via 7za.exe.
The archive contains macOS .dylib symlinks that can't be created on Windows
without administrator/Developer Mode privileges. The extraction succeeds for
all Windows-relevant files, but 7za exits with code 2, which app-builder
treats as a hard failure.

This wrapper runs the real 7za (installed next to itself as 7za-real.exe),
forwards all output, and remaps exit code 2 to 0 if stderr contains ONLY
benign symlink errors (specifically .dylib/libcrypto/libssl paths). Any
other error preserves the real exit code.
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(sys.argv[0]).resolve().parent
REAL = HERE / "7za-real.exe"


def looks_benign(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    hard_signals = [
        "data error", "crc failed", "decompression failed",
        "cannot open the file as",
        "unsupported method", "not implemented",
        "no space",
    ]
    if any(s in lowered for s in hard_signals):
        return False
    # Only mark as benign if the sole error lines mention symlinks to macOS libs
    bad_lines = [
        ln for ln in stderr_text.splitlines()
        if "error" in ln.lower() or "errors" in ln.lower()
    ]
    if not bad_lines:
        return True
    for ln in bad_lines:
        ll = ln.lower()
        if "symbolic link" in ll or "darwin" in ll or ".dylib" in ll:
            continue
        if "sub items errors" in ll or "archives with errors" in ll:
            continue
        return False
    return True


def main() -> int:
    if not REAL.exists():
        sys.stderr.write(f"wrapper error: real 7za not found at {REAL}\n")
        return 127
    args = [str(REAL), *sys.argv[1:]]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    rc = proc.returncode
    if rc == 2 and looks_benign(proc.stderr or ""):
        return 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
