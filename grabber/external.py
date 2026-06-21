"""Download files that course URL-modules link to (Google Drive, direct files).

A Moodle ``url`` module often points at an external resource — most commonly a
Google Drive file (e.g. a code notebook). We try to fetch the real file so it
lands next to the videos/presentations; if that fails the caller falls back to
saving a ``.url`` shortcut.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path

import requests

from .http_download import download_file
from .utils import filename_from_url, sanitize_filename

log = logging.getLogger(__name__)

_DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
_DRIVE_ID_RES = (
    re.compile(r"/d/([\w-]{20,})"),       # .../file/d/<ID>/view
    re.compile(r"[?&]id=([\w-]{20,})"),   # .../open?id=<ID> or uc?id=<ID>
)


def download_external(
    session: requests.Session,
    target_url: str,
    dest_dir: Path,
    prefix: str,
) -> Path | None:
    """Best-effort download of ``target_url`` into ``dest_dir``.

    Returns the saved file path, or ``None`` if it could not be downloaded
    (the caller then stores a ``.url`` shortcut instead).
    """
    if any(host in target_url for host in _DRIVE_HOSTS):
        return _download_google_drive(target_url, dest_dir, prefix)
    return _download_direct(session, target_url, dest_dir, prefix)


# -- Google Drive --------------------------------------------------------------
def _download_google_drive(url: str, dest_dir: Path, prefix: str) -> Path | None:
    file_id = _extract_drive_id(url)
    if not file_id:
        log.debug("could not extract Drive id from %s", url)
        return None
    try:
        import gdown
    except ImportError:
        log.warning("gdown not installed — cannot download Drive file %s", url)
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest_dir) as tmp:
        try:
            # gdown returns the path it wrote, using Drive's real file name.
            saved = gdown.download(id=file_id, output=f"{tmp}/", quiet=True)
        except Exception as exc:  # network / quota / not-a-file
            log.warning("  gdown failed for %s: %s", url, exc)
            return None
        if not saved:
            return None
        saved = Path(saved)
        final = dest_dir / f"{prefix} - {sanitize_filename(saved.name)}"
        shutil.move(str(saved), str(final))
    return final


def _extract_drive_id(url: str) -> str | None:
    for pattern in _DRIVE_ID_RES:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


# -- generic direct file -------------------------------------------------------
def _download_direct(
    session: requests.Session, url: str, dest_dir: Path, prefix: str
) -> Path | None:
    """Download ``url`` if it serves an actual file (not an HTML page)."""
    try:
        head = session.get(url, stream=True, timeout=30)
    except requests.RequestException as exc:
        log.debug("HEAD-like GET failed for %s: %s", url, exc)
        return None
    content_type = (head.headers.get("Content-Type") or "").lower()
    disposition = head.headers.get("Content-Disposition") or ""
    head.close()

    # An HTML response with no attachment disposition is a web page, not a file.
    if "text/html" in content_type and "attachment" not in disposition.lower():
        return None

    name = _disposition_name(disposition) or filename_from_url(url)
    if not name or "." not in name:
        return None
    dest = dest_dir / f"{prefix} - {sanitize_filename(name.rsplit('.', 1)[0])}." \
                      f"{name.rsplit('.', 1)[1].lower()}"
    try:
        download_file(session, head.url if hasattr(head, "url") else url, dest,
                      connections=4)
    except Exception as exc:
        log.warning("  direct download failed for %s: %s", url, exc)
        return None
    return dest


def _disposition_name(header: str) -> str | None:
    for part in header.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    return None
