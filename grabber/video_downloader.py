"""Download lecture videos at the highest available quality.

Two delivery kinds are handled:
  * ``rutube`` — via yt-dlp (picks the best quality, remuxes to mp4).
  * ``direct`` — mp4 served from Moodle, streamed over the auth session.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .http_download import download_file
from .models import VideoItem
from .moodle_client import MoodleClient
from .utils import filename_from_url, sanitize_filename

log = logging.getLogger(__name__)

_MAX_TRIES = 4
_RETRY_BASE_DELAY = 6  # seconds; multiplied by the attempt number (6s, 12s, 18s)

# Substrings of yt-dlp errors that are worth retrying (throttling / network /
# server hiccups) rather than giving up — as opposed to a genuinely gone video.
_TRANSIENT_MARKERS = (
    "no video formats found",
    "unable to download",
    "http error 429",
    "http error 5",
    "timed out",
    "timeout",
    "connection",
    "temporarily",
    "read operation",
    "no output file",
)


def _is_transient(message: str) -> bool:
    msg = message.lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


class VideoDownloader:
    """Routes a :class:`VideoItem` to the right download strategy."""

    def __init__(
        self,
        client: MoodleClient,
        video_format: str = "best",
        ffmpeg_location: str | None = None,
        skip_existing: bool = True,
        connections: int = 4,
    ):
        self.client = client
        self.video_format = video_format
        self.ffmpeg_location = ffmpeg_location
        self.skip_existing = skip_existing
        self.connections = connections

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
        ext = (ext or "mp4").lower()
        stem = sanitize_filename(f"{prefix} - {stem or raw}")
        dest = dest_dir / f"{stem}.{ext}"

        if dest.exists() and self.skip_existing:
            log.info("  ✓ video already downloaded: %s", dest.name)
            return dest

        download_file(
            self.client.session, video.url, dest, connections=self.connections
        )
        log.info("  ✓ saved video: %s", dest.name)
        return dest

    # -- rutube ----------------------------------------------------------------
    def _download_rutube(self, video: VideoItem, dest_dir: Path) -> Path | None:
        import yt_dlp

        prefix = f"{video.order:02d}"
        # Check for an existing file *before* touching rutube: a re-run then makes
        # zero API calls, which is also what avoids the rate-limiting that caused
        # intermittent "No video formats found" errors in the first place.
        existing = self._find_existing(dest_dir, prefix)
        if existing is not None and self.skip_existing:
            log.info("  ✓ video already downloaded: %s", existing.name)
            return existing

        # rutube throttles bursts of requests; a throttled response surfaces as a
        # transient extraction error. Retry a few times with growing backoff
        # instead of permanently dropping the video.
        for attempt in range(1, _MAX_TRIES + 1):
            try:
                info = self._extract_info(yt_dlp, video.url)
                video.title = info.get("title") or f"video_{video.order}"
                stem = sanitize_filename(f"{prefix} - {video.title}")
                outtmpl = str(dest_dir / f"{stem}.%(ext)s")
                with yt_dlp.YoutubeDL(self._ydl_opts(outtmpl)) as ydl:
                    result = ydl.extract_info(video.url, download=True)
                    path = Path(ydl.prepare_filename(result))
                final = path if path.exists() else self._find_existing(dest_dir, prefix)
                if final is not None:
                    log.info("  ✓ saved video: %s", final.name)
                    return final
                raise yt_dlp.utils.DownloadError("no output file produced")
            except yt_dlp.utils.DownloadError as exc:
                if attempt < _MAX_TRIES and _is_transient(str(exc)):
                    wait = _RETRY_BASE_DELAY * attempt
                    log.warning("    … rutube transient error (try %d/%d), "
                                "retrying in %ds", attempt, _MAX_TRIES, wait)
                    time.sleep(wait)
                    continue
                raise
        return None

    def _extract_info(self, yt_dlp_module, url: str) -> dict:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extractor_retries": 4}
        with yt_dlp_module.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _ydl_opts(self, outtmpl: str) -> dict:
        opts: dict = {
            "format": self.video_format,
            "outtmpl": outtmpl,
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 4,
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
