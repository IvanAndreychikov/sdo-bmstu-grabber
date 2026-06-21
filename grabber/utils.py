"""Small shared helpers."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape as _xml_escape

# Characters that are illegal in file names on at least one supported OS
# (this is the Windows set, which is the strictest — applying it everywhere
# keeps names valid on Windows, macOS and Linux alike).
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Device names Windows reserves regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Turn an arbitrary string into a cross-platform-safe file/folder name."""
    name = _ILLEGAL.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Windows forbids trailing dots/spaces; harmless to strip elsewhere.
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


def write_link_shortcut(dest_dir: Path, stem: str, name: str, url: str) -> Path:
    """Write a clickable link shortcut in the current OS's native format.

    Windows → ``.url`` (InternetShortcut), macOS → ``.webloc`` (plist),
    Linux/other → ``.desktop`` (Type=Link). The file is plain text containing
    the URL, so it remains readable on any platform regardless of format.
    """
    if sys.platform == "darwin":
        dest = dest_dir / f"{stem}.webloc"
        content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n\t<key>URL</key>\n'
            f"\t<string>{_xml_escape(url)}</string>\n</dict>\n</plist>\n"
        )
    elif sys.platform == "win32":
        dest = dest_dir / f"{stem}.url"
        content = f"[InternetShortcut]\nURL={url}\n"
    else:  # linux and other posix
        dest = dest_dir / f"{stem}.desktop"
        content = f"[Desktop Entry]\nType=Link\nName={name}\nURL={url}\n"
    dest.write_text(content, encoding="utf-8")
    return dest
