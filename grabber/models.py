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
class DocItem:
    """A module whose content is saved as a document (PDF, HTML fallback).

    Covers Moodle modules that are not a downloadable file or a video but still
    carry material worth keeping:
      * ``page``     — a ``mod/page`` text lesson (its rendered content);
      * ``quiz``     — a ``mod/quiz`` view page (description/instructions);
      * ``feedback`` — a ``mod/feedback`` view page;
      * ``assign``   — a ``mod/assign`` assignment description.
    Only the visible view page is captured — no attempt/submission is started.
    """

    url: str            # view URL of the module
    name: str           # human-readable activity name
    order: int          # 1-based position within its section
    kind: str = "page"  # "page" | "quiz" | "feedback" | "assign"


@dataclass
class SectionNode:
    """A node in the (possibly deeply nested) course-section tree."""

    number: int                 # Moodle section number (data-number)
    name: str
    parent: int | None = None   # parent section number, or None for top-level
    index: int = 0              # folder-order prefix (section number at the top
    #                             level, 1-based sibling order when nested)


@dataclass
class WebinarItem:
    """An MTS Link (webinar.ru) webinar recording embedded via a Moodle
    ``mod/mtslink`` module.

    Unlike a plain lecture video, the recording is not a single file: it is an
    event-log timeline of several HLS streams (speaker camera + audio, and one
    or more screen-shares). It is downloaded and composited into one mp4.
    """

    url: str            # view URL of the mtslink module
    name: str           # human-readable activity name (e.g. "Вебинар 1 от ...")
    order: int          # 1-based position within its section


@dataclass
class Section:
    """One course section/topic with its videos and files."""

    number: int
    name: str
    videos: list[VideoItem] = field(default_factory=list)
    files: list[FileItem] = field(default_factory=list)
    webinars: list[WebinarItem] = field(default_factory=list)
    docs: list[DocItem] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.videos or self.files or self.webinars or self.docs)
