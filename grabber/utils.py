"""Small shared helpers."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

# Characters illegal in Windows file/folder names.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Names Windows reserves regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Turn an arbitrary string into a safe Windows file/folder name."""
    name = _ILLEGAL.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Windows forbids trailing dots/spaces on names.
    name = name.rstrip(" .")
    if name.upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name or "untitled"


def filename_from_url(url: str) -> str:
    """Extract and url-decode the final path segment of a URL."""
    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1])
