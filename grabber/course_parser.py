"""Parse Moodle course/section HTML into the data models."""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, Tag

from .models import FileItem, Section, VideoItem
from .moodle_client import MoodleClient

log = logging.getLogger(__name__)

_RUTUBE_RE = re.compile(r"rutube\.ru/(?:play/embed|video)", re.IGNORECASE)
_VIDEO_EXT_RE = re.compile(r"\.(mp4|webm|m4v|mov)(?:$|\?)", re.IGNORECASE)


class CourseParser:
    """Reads course structure and per-section content from Moodle."""

    def __init__(self, client: MoodleClient, course_id: int):
        self.client = client
        self.course_id = course_id

    # -- course level ----------------------------------------------------------
    def course_name(self) -> str:
        """The human-readable course title (used as the top-level folder)."""
        soup = self.client.get_soup(f"/course/view.php?id={self.course_id}")
        for sel in (".page-header-headings h1", "#page-header h1", "header h1", "h1"):
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return f"course_{self.course_id}"

    def section_map(self) -> dict[int, str]:
        """Return ``{section_number: section_name}`` for the whole course."""
        soup = self.client.get_soup(f"/course/view.php?id={self.course_id}")
        result: dict[int, str] = {}
        for li in soup.select("li.section[data-number]"):
            number = li.get("data-number")
            if number is None or not number.isdigit():
                continue
            name_el = li.select_one(".sectionname")
            name = name_el.get_text(strip=True) if name_el else f"Раздел {number}"
            result[int(number)] = name
        return result

    # -- section level ---------------------------------------------------------
    def parse_section(self, number: int, name: str) -> Section:
        """Fetch one section page and extract videos and files in document order."""
        soup = self.client.get_soup(
            f"/course/view.php?id={self.course_id}&section={number}"
        )
        section = Section(number=number, name=name)

        order = 0
        for activity in soup.select("li.activity"):
            # 1) rutube embeds (sections 3-4)
            for url in self._rutube_embeds(activity):
                order += 1
                section.videos.append(
                    VideoItem(url=url, order=order, kind="rutube")
                )

            # 2) direct mp4 videos hosted on Moodle (sections 5+)
            for url in self._direct_videos(activity):
                order += 1
                section.videos.append(
                    VideoItem(url=url, order=order, kind="direct")
                )

            # 3) file resources (presentations)
            file_item = self._resource_file(activity, order + 1)
            if file_item is not None:
                order += 1
                section.files.append(file_item)
                continue

            # 4) external URL modules (e.g. code notebooks)
            url_item = self._url_module(activity, order + 1)
            if url_item is not None:
                order += 1
                section.files.append(url_item)

        log.info(
            "Section %s '%s': %d video(s), %d file(s)",
            number, name, len(section.videos), len(section.files),
        )
        return section

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _rutube_embeds(activity: Tag) -> list[str]:
        urls: list[str] = []
        for iframe in activity.find_all("iframe"):
            src = iframe.get("src") or ""
            if _RUTUBE_RE.search(src):
                urls.append(src)
        for anchor in activity.find_all("a", href=True):
            href = anchor["href"]
            if _RUTUBE_RE.search(href) and href not in urls:
                urls.append(href)
        return urls

    @staticmethod
    def _direct_videos(activity: Tag) -> list[str]:
        """Distinct mp4 URLs from <video>/<source>, in document order."""
        urls: list[str] = []
        for video in activity.find_all("video"):
            candidates = []
            if video.get("src"):
                candidates.append(video["src"])
            for source in video.find_all("source"):
                if source.get("src"):
                    candidates.append(source["src"])
            # one file per <video>: take the first playable source.
            for src in candidates:
                if _VIDEO_EXT_RE.search(src) and src not in urls:
                    urls.append(src)
                    break
        return urls

    @staticmethod
    def _resource_file(activity: Tag, order: int) -> FileItem | None:
        if "modtype_resource" not in activity.get("class", []):
            return None
        link = activity.select_one("a[href*='/mod/resource/view.php']")
        if link is None:
            return None
        name = CourseParser._activity_name(activity, default="Файл")
        name = re.sub(r"\s*(Файл|File)$", "", name).strip() or name
        return FileItem(url=link["href"], name=name, order=order, kind="resource")

    @staticmethod
    def _url_module(activity: Tag, order: int) -> FileItem | None:
        if "modtype_url" not in activity.get("class", []):
            return None
        link = activity.select_one("a[href*='/mod/url/view.php']")
        if link is None:
            return None
        name = CourseParser._activity_name(activity, default="Ссылка")
        name = re.sub(r"\s*(Гиперссылка|URL)$", "", name).strip() or name
        return FileItem(url=link["href"], name=name, order=order, kind="url")

    @staticmethod
    def _activity_name(activity: Tag, default: str) -> str:
        name_el = activity.select_one(".instancename")
        return name_el.get_text(strip=True) if name_el else default
