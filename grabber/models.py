"""Data models describing the course tree."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VideoItem:
    """A single lecture video within a section.

    Two delivery kinds exist on sdo.bmstu.ru:
      * ``rutube`` — embedded rutube player; ``url`` is the embed URL.
      * ``direct`` — mp4 served straight from Moodle (``pluginfile.php``);
        requires the authenticated session to fetch.
    """

    url: str
    order: int          # 1-based position within its section
    kind: str = "rutube"  # "rutube" | "direct"
    title: str | None = None  # resolved at download time


@dataclass
class FileItem:
    """A downloadable resource (presentation) or external link (notebook).

      * ``resource`` — Moodle file module (``mod/resource``); ``url`` is its view page.
      * ``url`` — Moodle URL module (``mod/url``); points to an external link.
    """

    url: str            # view URL of the module
    name: str           # human-readable activity name
    order: int          # 1-based position within its section
    kind: str = "resource"  # "resource" | "url"


@dataclass
class Section:
    """One course section/topic with its videos and files."""

    number: int
    name: str
    videos: list[VideoItem] = field(default_factory=list)
    files: list[FileItem] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.videos and not self.files
