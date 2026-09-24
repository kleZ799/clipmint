"""Where the files the app ships with are, from source or from a build.

A PyInstaller build unpacks its data under sys._MEIPASS; running from the
repo, the same folders sit under assets/. Every lookup tries the build first,
so a copy running from source never shadows what the build carries.
"""
import sys
from pathlib import Path
from typing import Optional


def asset_dir(name: str) -> Optional[Path]:
    """assets/<name>, wherever this copy of the app keeps it, or None."""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "assets" / name)
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / name)
    for path in candidates:
        if path.is_dir():
            return path
    return None
