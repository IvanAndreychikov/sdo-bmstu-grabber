"""Tie the pieces together: walk the course and download everything."""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .course_parser import CourseParser
from .file_downloader import FileDownloader
from .models import Section
from .moodle_client import MoodleClient
from .utils import sanitize_filename
from .video_downloader import VideoDownloader

log = logging.getLogger(__name__)


class CourseGrabber:
    """High-level workflow controller."""

    def __init__(self, config: Config):
        self.config = config
        self.client = MoodleClient(config.base_url)
        self.parser = CourseParser(self.client, config.course_id)
        self.videos = VideoDownloader(
            client=self.client,
            video_format=config.video_format,
            ffmpeg_location=_ffmpeg_location(),
            skip_existing=config.skip_existing,
        )
        self.files = FileDownloader(self.client, skip_existing=config.skip_existing)

    def run(self) -> None:
        cfg = self.config
        cfg.validate()
        output_dir = cfg.resolved_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)

        log.info("Logging in to %s ...", cfg.base_url)
        self.client.login(cfg.username, cfg.password)

        section_map = self.parser.section_map()
        targets = self._target_sections(section_map)
        log.info("Will process sections: %s", targets)

        for number in targets:
            section = self.parser.parse_section(number, section_map[number])
            if section.is_empty:
                log.info("Section %s is empty — skipping", number)
                continue
            self._process_section(section, output_dir)

        log.info("Done. Everything saved under: %s", output_dir)

    # -- internals -------------------------------------------------------------
    def _target_sections(self, section_map: dict[int, str]) -> list[int]:
        cfg = self.config
        available = sorted(n for n in section_map if n >= cfg.start_section)
        end = cfg.end_section if cfg.end_section is not None else (
            max(available) if available else cfg.start_section
        )
        return [n for n in available if n <= end]

    def _process_section(self, section: Section, output_dir: Path) -> None:
        folder = sanitize_filename(f"{section.number:02d} {section.name}")
        section_dir = output_dir / folder
        log.info("=== %s (%d video, %d file) ===",
                 folder, len(section.videos), len(section.files))

        for video in section.videos:
            try:
                self.videos.download(video, section_dir)
            except Exception as exc:  # keep going on a single bad item
                log.error("  ✗ video failed (%s): %s", video.embed_url, exc)

        for file_item in section.files:
            try:
                self.files.download(file_item, section_dir)
            except Exception as exc:
                log.error("  ✗ file failed (%s): %s", file_item.name, exc)


def _ffmpeg_location() -> str | None:
    """Locate the ffmpeg binary bundled by imageio-ffmpeg, if present."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None  # yt-dlp will fall back to system ffmpeg / native HLS
