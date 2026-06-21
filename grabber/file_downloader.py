"""Download file resources (presentations) and external link modules."""
from __future__ import annotations

import logging
from pathlib import Path

from bs4 import BeautifulSoup

from .external import download_external
from .http_download import download_file
from .models import FileItem
from .moodle_client import MoodleClient
from .utils import filename_from_url, sanitize_filename

log = logging.getLogger(__name__)


class FileDownloader:
    """Handles Moodle ``resource`` files and ``url`` (link) modules."""

    def __init__(
        self, client: MoodleClient, skip_existing: bool = True, connections: int = 4
    ):
        self.client = client
        self.skip_existing = skip_existing
        self.connections = connections

    def download(self, item: FileItem, dest_dir: Path) -> Path | None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if item.kind == "url":
            return self._save_link(item, dest_dir)
        return self._download_resource(item, dest_dir)

    # -- resource (file) -------------------------------------------------------
    def _download_resource(self, item: FileItem, dest_dir: Path) -> Path | None:
        prefix = f"{item.order:02d}"
        file_url, suggested = self._resolve_file_url(item.url)
        if file_url is None:
            log.warning("  ✗ no file found for resource: %s", item.name)
            return None

        filename = self._build_filename(prefix, item.name, suggested, file_url)
        dest = dest_dir / filename
        if dest.exists() and self.skip_existing:
            log.info("  ✓ file already downloaded: %s", dest.name)
            return dest

        download_file(
            self.client.session, file_url, dest, connections=self.connections
        )
        log.info("  ✓ saved file: %s", dest.name)
        return dest

    def _resolve_file_url(self, view_url: str) -> tuple[str | None, str | None]:
        """Return ``(direct_file_url, content_disposition_name)``."""
        resp = self.client.get(view_url)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type:
            return resp.url, _disposition_name(resp.headers.get("Content-Disposition"))

        soup = BeautifulSoup(resp.text, "lxml")
        link = soup.select_one(
            ".resourceworkaround a[href], .resourcecontent a[href], "
            "object[data], a[href*='pluginfile.php']"
        )
        if link is not None:
            href = link.get("href") or link.get("data")
            if href:
                return href, None
        return None, None

    @staticmethod
    def _build_filename(
        prefix: str, activity_name: str, suggested: str | None, file_url: str
    ) -> str:
        raw = suggested or filename_from_url(file_url)
        if "." in raw:
            stem, ext = raw.rsplit(".", 1)
            return f"{prefix} - {sanitize_filename(stem)}.{ext.lower()}"
        return f"{prefix} - {sanitize_filename(activity_name)}"

    # -- url (external link) ---------------------------------------------------
    def _save_link(self, item: FileItem, dest_dir: Path) -> Path | None:
        """Resolve a URL module: download the real file it points at, and only
        fall back to a ``.url`` shortcut when the file can't be fetched.

        These links commonly target a Google Drive file (e.g. a code notebook);
        we pull the actual file so it sits next to the videos/presentations.
        """
        prefix = f"{item.order:02d}"
        target = self._resolve_external_url(item.url)
        if target is None:
            log.warning("  ✗ no link found for url module: %s", item.name)
            return None

        existing = self._find_existing(dest_dir, prefix)
        if existing is not None and self.skip_existing:
            log.info("  ✓ link target already downloaded: %s", existing.name)
            return existing

        # Primary strategy: download the actual linked file.
        downloaded = download_external(
            self.client.session, target, dest_dir, prefix
        )
        if downloaded is not None:
            log.info("  ✓ saved linked file: %s", downloaded.name)
            return downloaded

        # Fallback: keep the link as a Windows .url shortcut.
        dest = dest_dir / f"{prefix} - {sanitize_filename(item.name)}.url"
        dest.write_text(f"[InternetShortcut]\nURL={target}\n", encoding="utf-8")
        log.info("  ✓ saved link shortcut (download unavailable): %s -> %s",
                 dest.name, target)
        return dest

    @staticmethod
    def _find_existing(dest_dir: Path, prefix: str) -> Path | None:
        if not dest_dir.exists():
            return None
        for path in sorted(dest_dir.glob(f"{prefix} - *")):
            if path.is_file() and not path.name.endswith(".part"):
                return path
        return None

    def _resolve_external_url(self, view_url: str) -> str | None:
        resp = self.client.get(view_url)
        # If Moodle redirected straight to the target, use the final URL.
        if "/mod/url/view.php" not in resp.url:
            return resp.url
        soup = BeautifulSoup(resp.text, "lxml")
        link = soup.select_one(".urlworkaround a[href], .resourcecontent a[href]")
        if link is None:
            # fall back to the first off-site link on the page
            for a in soup.select("a[href^='http']"):
                href = a["href"]
                if "sdo.bmstu.ru" not in href:
                    link = a
                    break
        return link["href"] if link else None


def _disposition_name(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    return None
