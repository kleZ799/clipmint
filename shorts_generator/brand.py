"""The channel's own mark, put in the corner of every clip.

A clip that travels -- reposted, stitched, screen-recorded -- takes the
creator's name with it only if the name is in the picture. So a logo uploaded
once in Settings is laid over every clip the edit touches, small and a little
see-through, in the corner the creator picked.

The image lives in the app's settings folder rather than beside the clips: it
belongs to the channel, not to any one run.
"""
from pathlib import Path
from typing import Optional

from . import user_config

CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
DEFAULT_CORNER = "top-right"
CORNER_KEY = "BRAND_LOGO_CORNER"

# PNG and JPEG are what every logo export tool writes, and both are read by
# ffmpeg without anything extra. Checked by their first bytes, never by name.
_MAGIC = {b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg"}
MAX_BYTES = 5 * 1024 * 1024

# How big the mark is, as a share of the frame's shorter side, and how solid.
SIZE = 0.16
OPACITY = 0.85


def _folder() -> Path:
    return user_config.config_dir() / "brand"


def logo_path() -> Optional[Path]:
    """The stored logo, if there is one."""
    for ext in (".png", ".jpg"):
        p = _folder() / f"logo{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def corner() -> str:
    value = user_config.get(CORNER_KEY, DEFAULT_CORNER).strip().lower()
    return value if value in CORNERS else DEFAULT_CORNER


def set_corner(value: str) -> str:
    value = (value or "").strip().lower()
    if value not in CORNERS:
        raise ValueError(f"The corner must be one of: {', '.join(CORNERS)}.")
    user_config.save({CORNER_KEY: value})
    return value


def image_kind(data: bytes) -> Optional[str]:
    """".png" or ".jpg" for an image this can use, from its bytes."""
    for magic, ext in _MAGIC.items():
        if data.startswith(magic):
            return ext
    return None


def save_logo(data: bytes) -> Path:
    """Store a new logo, replacing any old one. Raises ValueError on a bad file."""
    if len(data) > MAX_BYTES:
        raise ValueError("That image is over 5 MB. A logo needs far less; export it smaller.")
    ext = image_kind(data)
    if ext is None:
        raise ValueError("That isn't a PNG or a JPEG. Export the logo as a PNG, "
                         "ideally with a transparent background.")
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    remove_logo()
    path = folder / f"logo{ext}"
    tmp = path.with_suffix(ext + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def remove_logo() -> bool:
    removed = False
    for ext in (".png", ".jpg"):
        p = _folder() / f"logo{ext}"
        if p.exists():
            p.unlink()
            removed = True
    return removed
