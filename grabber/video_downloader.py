"""Download lecture videos at the highest available quality.

Two delivery kinds are handled:
  * ``rutube`` — via yt-dlp (picks the best quality, remuxes to mp4).
  * ``direct`` — mp4 served from Moodle, streamed over the auth session.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .http_download import stream_download
from .models import VideoItem
from .moodle_client import MoodleClient
from .utils import filename_from_url, sanitize_filename

log = logging.getLogger(__name__)


class VideoDownloader:
    """Routes a :class:`VideoItem` to the right download strategy."""

    def __init__(
        self,
        client: MoodleClient,
        video_format: str = "best",
        ffmpeg_location: str | None = None,
        skip_existing: bool = True,
    ):
        self.client = client
        self.video_format = video_format
        self.ffmpeg_location = ffmpeg_location
        self.skip_existing = skip_existing

    def download(self, video: VideoItem, dest_dir: Path) -> Path | None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if video.kind == "direct":
            return self._download_direct(video, dest_dir)
        return self._download_rutube(video, dest_dir)

    # -- direct (Moodle-hosted mp4) --------------------------------------------
    def _download_direct(self, video: VideoItem, dest_dir: Path) -> Path | None:
        prefix = f"{video.order:02d}"
        # Filenames like "402.1.mp4" carry no human title, so keep the original
        # stem (it encodes topic/part) alongside the order prefix.
        raw = filename_from_url(video.url) or f"video_{video.order}.mp4"
        stem, _, ext = raw.rpartition(".")
        stem = sanitize_filename(stem or raw)
        ext = (ext or "mp4").lower()
        dest = dest_dir / f"{prefix} - {stem}.{ext}"

        if dest.exists() and self.skip_existing:
            log.info("  ✓ video already downloaded: %s", dest.name)
            return dest

        stream_download(self.client.session, video.url, dest)
        log.info("  ✓ saved video: %s", dest.name)
        return dest

    # -- rutube ----------------------------------------------------------------
    def _download_rutube(self, video: VideoItem, dest_dir: Path) -> Path | None:
        import yt_dlp

        prefix = f"{video.order:02d}"
        info = self._extract_info(yt_dlp, video.url)
        video.title = info.get("title") or f"video_{video.order}"
        stem = sanitize_filename(f"{prefix} - {video.title}")

        existing = self._find_existing(dest_dir, prefix)
        if existing is not None and self.skip_existing:
            log.info("  ✓ video already downloaded: %s", existing.name)
            return existing

        outtmpl = str(dest_dir / f"{stem}.%(ext)s")
        with yt_dlp.YoutubeDL(self._ydl_opts(outtmpl)) as ydl:
            result = ydl.extract_info(video.url, download=True)
            path = Path(ydl.prepare_filename(result))

        final = path if path.exists() else self._find_existing(dest_dir, prefix)
        if final is not None:
            log.info("  ✓ saved video: %s", final.name)
        return final

    def _extract_info(self, yt_dlp_module, url: str) -> dict:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp_module.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _ydl_opts(self, outtmpl: str) -> dict:
        opts: dict = {
            "format": self.video_format,
            "outtmpl": outtmpl,
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "merge_output_format": "mp4",
            "logger": _YdlLogger(),
        }
        if self.ffmpeg_location:
            opts["ffmpeg_location"] = self.ffmpeg_location
        return opts

    @staticmethod
    def _find_existing(dest_dir: Path, prefix: str) -> Path | None:
        for path in sorted(dest_dir.glob(f"{prefix} - *")):
            if path.is_file() and not path.name.endswith(".part"):
                return path
        return None


class _YdlLogger:
    """Route yt-dlp's chatter into our logger at a quiet level."""

    def debug(self, msg):
        if not msg.startswith("[debug]"):
            log.debug(msg)

    def info(self, msg):
        log.debug(msg)

    def warning(self, msg):
        # rutube variants are already muxed and we pick the best single stream,
        # so the missing-ffprobe metadata probe is harmless — don't spam it.
        if "ffprobe" in msg or "ffmpeg" in msg.lower():
            log.debug("yt-dlp: %s", msg)
            return
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)
