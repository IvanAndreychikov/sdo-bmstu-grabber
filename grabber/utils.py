"""Small shared helpers."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape as _xml_escape

# Characters that are illegal in file names on at least one supported OS
# (this is the Windows set, which is the strictest — applying it everywhere
# keeps names valid on Windows, macOS and Linux alike). The C1 control range
# (\x7f-\x9f) is included: those bytes only ever appear here as the wreckage of
# mis-decoded UTF-8, and some of them (\x85, \xa0) are Unicode whitespace that a
# naive ``\s`` collapse would silently eat mid-character.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')
# Device names Windows reserves regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def fix_mojibake(text: str) -> str:
    """Repair text that was UTF-8 but got decoded as Latin-1 (``ÐÐ±Ð»Ð°ÑÐ½Ð°Ñ``).

    HTTP headers are Latin-1 by spec, so a UTF-8 filename in a
    ``Content-Disposition`` header comes back as garbled ``Ð…``/``Ñ…`` sequences.
    Re-encoding to Latin-1 and decoding as UTF-8 recovers the original. The repair
    is only accepted when the input has *no* Cyrillic yet the result *does* — so
    legitimate ASCII or already-correct Cyrillic names are never touched.
    """
    if not text or text.isascii():
        return text
    if any("Ѐ" <= c <= "ӿ" for c in text):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if any("Ѐ" <= c <= "ӿ" for c in repaired):
        return repaired
    return text


def disposition_filename(header: str | None) -> str | None:
    """Extract the filename from a ``Content-Disposition`` header, decoded right.

    Prefers the RFC 5987 extended form (``filename*=UTF-8''…``, percent-encoded
    and unambiguous) and otherwise repairs the plain ``filename=`` form, whose
    value arrives Latin-1-decoded and so is mojibake for any UTF-8 name.
    """
    if not header:
        return None
    m = re.search(r"filename\*\s*=\s*([^;]+)", header, re.IGNORECASE)
    if m:
        val = m.group(1).strip().strip('"')
        if "''" in val:
            charset, _, enc = val.split("'", 2)
            try:
                return unquote(enc, encoding=charset or "utf-8", errors="replace")
            except (LookupError, ValueError):
                pass
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', header, re.IGNORECASE)
    if m:
        return fix_mojibake(m.group(1).strip())
    return None


# Max *characters* for one path component. Kept short by default so that deeply
# nested paths (course → module → topic → lesson → file) stay under Windows'
# 260-char MAX_PATH — many media players and Explorer use the legacy API and
# cannot open longer paths even when the OS long-path option is enabled. With
# this course's nesting, 36 keeps the longest path at ~249. Callers fold the
# order prefix ("NN - ") into the name *before* sanitizing, so this single cap
# bounds the whole component. Configurable via ``config.max_name_length`` (set
# through :func:`set_max_name_length`); 0 turns the character cap off.
_MAX_NAME_LENGTH = 36
# Max *bytes* for one path component (Linux/ext4 caps at 255 bytes; Cyrillic is
# 2 B/char). This always applies — even with the character cap off — so names
# stay valid on Linux regardless of the user's setting.
_MAX_NAME_BYTES = 250


def set_max_name_length(chars: int) -> None:
    """Set the global character cap used by :func:`sanitize_filename`.

    Called once at startup from the loaded config. ``chars <= 0`` disables the
    character cap (full names; only the byte cap remains for Linux safety).
    """
    global _MAX_NAME_LENGTH
    _MAX_NAME_LENGTH = chars


def sanitize_filename(name: str, max_length: int | None = None) -> str:
    """Turn an arbitrary string into a cross-platform-safe file/folder name.

    ``max_length`` defaults to the configured global cap; pass an explicit value
    to override it for one call. A value ``<= 0`` skips the character cap (only
    the byte cap, for Linux filesystem limits, still applies).
    """
    if max_length is None:
        max_length = _MAX_NAME_LENGTH
    name = fix_mojibake(name)
    name = _ILLEGAL.sub(" ", name)
    # Collapse ASCII whitespace only: a Unicode ``\s`` would also match NBSP /
    # NEL, which are the second byte of common Cyrillic letters in mojibake.
    name = re.sub(r"[ \t\n\r\f\v]+", " ", name).strip()
    # Windows forbids trailing dots/spaces; harmless to strip elsewhere.
    name = name.rstrip(" .")
    if name.upper() in _RESERVED:
        name = f"_{name}"
    if max_length > 0 and len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        # Truncate on a byte boundary without splitting a multi-byte character.
        name = name.encode("utf-8")[:_MAX_NAME_BYTES].decode("utf-8", "ignore")
        name = name.rstrip(" .")
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
