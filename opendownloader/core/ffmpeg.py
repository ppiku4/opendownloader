"""Locate and call ffmpeg for remuxing / merging media (no re-encoding)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_ffmpeg(configured: str = "") -> str | None:
    if configured and Path(configured).is_file():
        return configured
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    from .tools import installed_path

    app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else here
    for cand in (here / "ffmpeg" / exe, here / exe, app_dir / exe, installed_path("ffmpeg")):
        if cand is not None and cand.is_file():
            return str(cand)
    return shutil.which("ffmpeg")


def run_ffmpeg(ffmpeg: str, inputs: list[Path], output: Path) -> None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for p in inputs:
        cmd += ["-i", str(p)]
    for i in range(len(inputs)):
        cmd += ["-map", f"{i}"]
    cmd += ["-c", "copy"]
    if output.suffix.lower() in (".mp4", ".m4a", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(output))
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags,
                          env={**os.environ})
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-500:]}")
