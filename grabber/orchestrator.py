"""Tie the pieces together: walk the course and download everything."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .course_parser import CourseParser
from .file_downloader import FileDownloader
from .models import FileItem, Section, VideoItem
from .moodle_client import MoodleClient
from .utils import sanitize_filename
from .video_downloader import VideoDownloader

log = logging.getLogger(__name__)


@dataclass
class _Job:
    """A single download unit, ready to run on a worker thread."""

    label: str               # for logging, e.g. "05/02 405.2.mp4"
    run: Callable[[], object]


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
            connections=config.concurrency,
        )
        self.files = FileDownloader(
            self.client,
            skip_existing=config.skip_existing,
            connections=config.concurrency,
        )

    def run(self) -> None:
        cfg = self.config
        cfg.validate()
        output_dir = cfg.resolved_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)

        log.info("Logging in to %s ...", cfg.base_url)
        self.client.login(cfg.username, cfg.password)

        # Everything lands under a top-level folder named after the course.
        course_name = self.parser.course_name()
        course_dir = output_dir / sanitize_filename(course_name)
        course_dir.mkdir(parents=True, exist_ok=True)
        log.info("Course: %s", course_name)

        section_map = self.parser.section_map()
        targets = self._target_sections(section_map)
        log.info("Will process sections: %s", targets)

        # Parse first so we can schedule every download together and keep all
        # worker threads busy (the server throttles a single stream).
        jobs = self._collect_jobs(targets, section_map, course_dir)
        log.info("Collected %d download job(s); %d connection(s) per file",
                 len(jobs), cfg.concurrency)

        self._run_jobs(jobs)
        log.info("Done. Everything saved under: %s", output_dir)

    # -- planning --------------------------------------------------------------
    def _target_sections(self, section_map: dict[int, str]) -> list[int]:
        cfg = self.config
        available = sorted(n for n in section_map if n >= cfg.start_section)
        end = cfg.end_section if cfg.end_section is not None else (
            max(available) if available else cfg.start_section
        )
        return [n for n in available if n <= end]

    def _collect_jobs(
        self,
        targets: list[int],
        section_map: dict[int, str],
        base_dir: Path,
    ) -> list[_Job]:
        jobs: list[_Job] = []
        for number in targets:
            section = self.parser.parse_section(number, section_map[number])
            if section.is_empty:
                log.info("Section %s is empty — skipping", number)
                continue
            section_dir = base_dir / sanitize_filename(
                f"{section.number:02d} {section.name}"
            )
            for video in section.videos:
                jobs.append(self._video_job(section, video, section_dir))
            for file_item in section.files:
                jobs.append(self._file_job(section, file_item, section_dir))
        return jobs

    def _video_job(self, section: Section, video: VideoItem, section_dir: Path) -> _Job:
        label = f"{section.number:02d}/{video.order:02d} [{video.kind}]"
        return _Job(label, lambda: self.videos.download(video, section_dir))

    def _file_job(self, section: Section, item: FileItem, section_dir: Path) -> _Job:
        label = f"{section.number:02d}/{item.order:02d} [{item.kind}] {item.name}"
        return _Job(label, lambda: self.files.download(item, section_dir))

    # -- execution -------------------------------------------------------------
    def _run_jobs(self, jobs: list[_Job]) -> None:
        # Jobs run sequentially: each large file already uses N parallel
        # connections internally (see http_download), so the active connection
        # count stays at N for every file — including the very last one, which
        # is where file-level parallelism used to collapse to a single stream.
        total = len(jobs)
        failed = 0
        for done, job in enumerate(jobs, start=1):
            try:
                job.run()
            except Exception as exc:
                failed += 1
                log.error("  ✗ [%d/%d] %s failed: %s", done, total, job.label, exc)
            else:
                log.info("  ● [%d/%d] %s ok", done, total, job.label)
        if failed:
            log.warning("%d of %d job(s) failed — see errors above", failed, total)


def _ffmpeg_location() -> str | None:
    """Locate the ffmpeg binary bundled by imageio-ffmpeg, if present."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None  # yt-dlp will fall back to system ffmpeg / native HLS
