"""Tie the pieces together: walk the course and download everything."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .course_parser import CourseParser
from .doc_downloader import DocDownloader
from .file_downloader import FileDownloader
from .models import DocItem, FileItem, Section, VideoItem, WebinarItem
from .moodle_client import MoodleClient
from .utils import sanitize_filename, set_max_name_length
from .video_downloader import VideoDownloader
from .webinar import WebinarDownloader

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
        # Apply the configured filename length cap before anything builds a path.
        set_max_name_length(config.max_name_length)
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
        self.docs = DocDownloader(self.client, skip_existing=config.skip_existing)
        ffmpeg = _ffmpeg_location()
        self.webinars = (
            WebinarDownloader(
                self.client,
                ffmpeg=ffmpeg,
                skip_existing=config.skip_existing,
                width=config.webinar_width,
                height=config.webinar_height,
                encoder=config.webinar_encoder,
                cpu_fraction=config.webinar_cpu_fraction,
            )
            if ffmpeg
            else None
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

        # Discover the full (possibly deeply nested) section tree, then parse and
        # schedule everything together so worker threads stay busy.
        tree = self.parser.section_tree(cfg.start_section, cfg.end_section)
        log.info("Discovered %d section(s) (including nested subsections)",
                 len(tree))
        jobs = self._collect_jobs(tree, course_dir)
        log.info("Collected %d download job(s); %d connection(s) per file",
                 len(jobs), cfg.concurrency)

        self._run_jobs(jobs)
        log.info("Done. Everything saved under: %s", output_dir)

    # -- planning --------------------------------------------------------------
    def _collect_jobs(self, tree, base_dir: Path) -> list[_Job]:
        """Parse every section in the tree and turn its content into jobs.

        Section folders mirror the course nesting; a section's directory is its
        parent's directory plus its own ``NN name`` (top-level sections keep
        their section-number prefix, nested ones use their sibling order).
        ``seen_mods`` dedups modules across the whole course.
        """
        jobs: list[_Job] = []
        dirs: dict[int, Path] = {}
        seen_mods: set[int] = set()
        for node in tree:
            parent_dir = base_dir if node.parent is None else dirs.get(
                node.parent, base_dir)
            section_dir = parent_dir / sanitize_filename(
                f"{node.index:02d} {node.name}"
            )
            dirs[node.number] = section_dir

            section = self.parser.parse_section(node.number, node.name, seen_mods)
            if section.is_empty:
                continue
            for video in section.videos:
                jobs.append(self._video_job(section, video, section_dir))
            for file_item in section.files:
                jobs.append(self._file_job(section, file_item, section_dir))
            for webinar in section.webinars:
                if self.webinars is None:
                    log.warning("Skipping webinar '%s' — ffmpeg unavailable",
                                webinar.name)
                    continue
                jobs.append(self._webinar_job(section, webinar, section_dir))
            for doc in section.docs:
                jobs.append(self._doc_job(section, doc, section_dir))
        return jobs

    def _video_job(self, section: Section, video: VideoItem, section_dir: Path) -> _Job:
        label = f"{section.number:02d}/{video.order:02d} [{video.kind}]"
        return _Job(label, lambda: self.videos.download(video, section_dir))

    def _file_job(self, section: Section, item: FileItem, section_dir: Path) -> _Job:
        label = f"{section.number:02d}/{item.order:02d} [{item.kind}] {item.name}"
        return _Job(label, lambda: self.files.download(item, section_dir))

    def _webinar_job(
        self, section: Section, item: WebinarItem, section_dir: Path
    ) -> _Job:
        label = f"{section.number:02d}/{item.order:02d} [webinar] {item.name}"
        return _Job(label, lambda: self.webinars.download(item, section_dir))

    def _doc_job(self, section: Section, item: DocItem, section_dir: Path) -> _Job:
        label = f"{section.number:02d}/{item.order:02d} [{item.kind}] {item.name}"
        return _Job(label, lambda: self.docs.download(item, section_dir))

    # -- execution -------------------------------------------------------------
    def _run_jobs(self, jobs: list[_Job]) -> None:
        # Jobs run sequentially: each large file already uses N parallel
        # connections internally (see http_download), so the active connection
        # count stays at N for every file — including the very last one, which
        # is where file-level parallelism used to collapse to a single stream.
        total = len(jobs)
        failed = 0
        for done, job in enumerate(jobs, start=1):
            # Announce the start of every item so progress is visible while a
            # large download or a webinar's local processing is under way.
            log.info("  ▶ [%d/%d] %s — START", done, total, job.label)
            try:
                result = job.run()
            except Exception as exc:
                failed += 1
                log.error("  ✗ [%d/%d] %s — FAILED: %s", done, total, job.label, exc)
                continue
            if result is None:
                failed += 1
                log.warning("  ✗ [%d/%d] %s — FAILED (no file produced)",
                            done, total, job.label)
            else:
                log.info("  ✓ [%d/%d] %s — DONE", done, total, job.label)
        if failed:
            log.warning("%d of %d job(s) failed — see messages above", failed, total)


def _ffmpeg_location() -> str | None:
    """Locate the ffmpeg binary bundled by imageio-ffmpeg, if present."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None  # yt-dlp will fall back to system ffmpeg / native HLS
