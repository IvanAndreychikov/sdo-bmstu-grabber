"""Parse Moodle course/section HTML into the data models.

The course uses Moodle's **flexible sections** format, where sections can be
nested arbitrarily deep (a top-level "Материалы курса" holds modules, each module
holds topics, each topic holds individual lessons). The collapsed main page does
not reveal nested content, so the tree is discovered by visiting each section
page and following its child subsections (:meth:`CourseParser.section_tree`).

Within a section, the actual lesson video is usually embedded inside a
``mod/page`` activity rather than inline, so pages are opened and their rutube /
direct videos, file links and text are extracted (:meth:`_parse_page`).
"""
from __future__ import annotations

import logging
import re
from collections import deque

from bs4 import BeautifulSoup, Tag

from .models import DocItem, FileItem, Section, SectionNode, VideoItem, WebinarItem
from .moodle_client import MoodleClient

log = logging.getLogger(__name__)

_RUTUBE_RE = re.compile(r"rutube\.ru/(?:play/embed|video)", re.IGNORECASE)
_VIDEO_EXT_RE = re.compile(r"\.(mp4|webm|m4v|mov)(?:$|\?)", re.IGNORECASE)
_MODID_RE = re.compile(r"/mod/\w+/view\.php\?id=(\d+)")
# A page is also saved as a document when it carries this much real text (short
# blurbs under a video — captions/titles — are not worth a PDF of their own).
_PAGE_TEXT_THRESHOLD = 300


class CourseParser:
    """Reads course structure and per-section content from Moodle."""

    def __init__(self, client: MoodleClient, course_id: int):
        self.client = client
        self.course_id = course_id
        self._soup_cache: dict[int, BeautifulSoup] = {}

    # -- course level ----------------------------------------------------------
    def course_name(self) -> str:
        """The human-readable course title (used as the top-level folder)."""
        soup = self.client.get_soup(f"/course/view.php?id={self.course_id}")
        for sel in (".page-header-headings h1", "#page-header h1", "header h1", "h1"):
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return f"course_{self.course_id}"

    def section_tree(
        self, start_section: int = 0, end_section: int | None = None
    ) -> list[SectionNode]:
        """Discover every section, descending into nested subsections.

        Top-level sections are filtered by ``start_section``/``end_section``;
        their (arbitrarily deep) descendants are always included. Breadth-first
        with first-discovery parenting makes the traversal robust against
        flexsections rendering a leaf page in its parent's context: any section
        it re-lists has already been seen and is ignored.
        """
        main = self._section_soup(0, url=f"/course/view.php?id={self.course_id}")
        nodes: dict[int, SectionNode] = {}
        order: list[int] = []
        queue: deque[int] = deque()
        for num, name in _child_sections(main, depth=0):
            if num < start_section or (end_section is not None and num > end_section):
                continue
            nodes[num] = SectionNode(num, name, parent=None, index=num)
            order.append(num)
            queue.append(num)

        seen: set[int] = set()
        while queue:
            sec = queue.popleft()
            if sec in seen:
                continue
            seen.add(sec)
            soup = self._section_soup(sec)
            for cidx, (cnum, cname) in enumerate(_child_sections(soup, depth=1, owner=sec), 1):
                if cnum not in nodes:
                    nodes[cnum] = SectionNode(cnum, cname, parent=sec, index=cidx)
                    order.append(cnum)
                    queue.append(cnum)
        return [nodes[n] for n in order]

    # -- section level ---------------------------------------------------------
    def parse_section(
        self, number: int, name: str, seen_mods: set[int] | None = None
    ) -> Section:
        """Extract a section's *own* content (videos, files, webinars, docs).

        Only activities belonging directly to this section are taken (those whose
        nearest section ancestor is ``number``), so a page that renders nested
        subsections does not pull their activities in. ``seen_mods`` dedups
        modules globally across the whole course.
        """
        seen_mods = seen_mods if seen_mods is not None else set()
        soup = self._section_soup(number)
        section = Section(number=number, name=name)
        order = 0

        for activity in _own_activities(soup, number):
            mid = _module_id(activity)
            if mid is not None:
                if mid in seen_mods:
                    continue
                seen_mods.add(mid)
            classes = activity.get("class", [])

            # Inline videos (course-86 style: rutube/<video> embedded in a label).
            for url in self._rutube_embeds(activity):
                order += 1
                section.videos.append(VideoItem(url=url, order=order, kind="rutube"))
            for url in self._direct_videos(activity):
                order += 1
                section.videos.append(VideoItem(url=url, order=order, kind="direct"))

            if "modtype_resource" in classes:
                item = self._resource_file(activity, order + 1)
                if item is not None:
                    order += 1
                    section.files.append(item)
            elif "modtype_url" in classes:
                item = self._url_module(activity, order + 1)
                if item is not None:
                    order += 1
                    section.files.append(item)
            elif "modtype_mtslink" in classes:
                webinar = self._webinar_module(activity, order + 1)
                if webinar is not None:
                    order += 1
                    section.webinars.append(webinar)
            elif "modtype_page" in classes:
                order = self._parse_page(activity, section, order)
            elif any(f"modtype_{k}" in classes for k in ("quiz", "feedback", "assign")):
                doc = self._doc_module(activity, order + 1)
                if doc is not None:
                    order += 1
                    section.docs.append(doc)

        log.info(
            "Section %s '%s': %d video(s), %d file(s), %d webinar(s), %d doc(s)",
            number, name, len(section.videos), len(section.files),
            len(section.webinars), len(section.docs),
        )
        return section

    # -- page handling ---------------------------------------------------------
    def _parse_page(self, activity: Tag, section: Section, order: int) -> int:
        """Open a ``mod/page`` activity and harvest its videos / files / text.

        A page typically wraps the lesson's rutube video(s); some pages instead
        (or also) hold written material. Videos become :class:`VideoItem`s, any
        embedded downloadable files become :class:`FileItem`s, and the page is
        additionally kept as a document when it carries real text (or has no
        video at all, so nothing is silently lost).
        """
        link = activity.select_one("a[href*='/mod/page/view.php']")
        if link is None:
            return order
        view_url = link["href"]
        name = self._activity_name(activity, default="Страница")
        try:
            page = self.client.get_soup(view_url)
        except Exception as exc:  # network/parse hiccup — keep going
            log.warning("    could not open page '%s': %s", name, exc)
            return order
        main = (page.select_one("[role='main']") or page.select_one("#region-main")
                or page)

        videos = self._rutube_embeds(main) + self._direct_video_urls(main)
        for url in videos:
            order += 1
            kind = "rutube" if _RUTUBE_RE.search(url) else "direct"
            section.videos.append(VideoItem(url=url, order=order, kind=kind))

        for fi in self._embedded_files(main, order_start=order + 1):
            order += 1
            section.files.append(fi)

        for tag in main.select("script, style"):
            tag.extract()
        text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
        if not videos or len(text) >= _PAGE_TEXT_THRESHOLD:
            order += 1
            section.docs.append(DocItem(url=view_url, name=name, order=order, kind="page"))
        return order

    def _embedded_files(self, main: Tag, order_start: int) -> list[FileItem]:
        """Downloadable links found inside a page body (pluginfile / Drive)."""
        out: list[FileItem] = []
        seen: set[str] = set()
        order = order_start
        for a in main.find_all("a", href=True):
            href = a["href"]
            is_plugin = "pluginfile.php" in href
            is_drive = "drive.google.com" in href or "docs.google.com" in href
            if not (is_plugin or is_drive) or href in seen:
                continue
            seen.add(href)
            label = a.get_text(strip=True) or "Файл"
            out.append(FileItem(url=href, name=label, order=order,
                                kind="url" if is_drive else "resource"))
            order += 1
        return out

    # -- helpers ---------------------------------------------------------------
    def _section_soup(self, number: int, url: str | None = None) -> BeautifulSoup:
        """Fetch (and cache) a section page so the tree walk and the content
        parse don't request it twice."""
        if number not in self._soup_cache:
            url = url or f"/course/view.php?id={self.course_id}&section={number}"
            self._soup_cache[number] = self.client.get_soup(url)
        return self._soup_cache[number]

    @staticmethod
    def _rutube_embeds(scope: Tag) -> list[str]:
        urls: list[str] = []
        for iframe in scope.find_all("iframe"):
            src = iframe.get("src") or ""
            if _RUTUBE_RE.search(src) and src not in urls:
                urls.append(src)
        for anchor in scope.find_all("a", href=True):
            href = anchor["href"]
            if _RUTUBE_RE.search(href) and href not in urls:
                urls.append(href)
        return urls

    @staticmethod
    def _direct_videos(activity: Tag) -> list[str]:
        """Distinct mp4 URLs from <video>/<source>, in document order."""
        return CourseParser._direct_video_urls(activity)

    @staticmethod
    def _direct_video_urls(scope: Tag) -> list[str]:
        urls: list[str] = []
        for video in scope.find_all("video"):
            candidates = []
            if video.get("src"):
                candidates.append(video["src"])
            for source in video.find_all("source"):
                if source.get("src"):
                    candidates.append(source["src"])
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
    def _webinar_module(activity: Tag, order: int) -> WebinarItem | None:
        if "modtype_mtslink" not in activity.get("class", []):
            return None
        link = activity.select_one("a[href*='/mod/mtslink/view.php']")
        if link is None:
            return None
        name = CourseParser._activity_name(activity, default="Вебинар")
        return WebinarItem(url=link["href"], name=name, order=order)

    @staticmethod
    def _doc_module(activity: Tag, order: int) -> DocItem | None:
        classes = activity.get("class", [])
        for kind in ("quiz", "feedback", "assign"):
            if f"modtype_{kind}" in classes:
                link = activity.select_one(f"a[href*='/mod/{kind}/view.php']")
                if link is None:
                    return None
                name = CourseParser._activity_name(activity, default=kind)
                return DocItem(url=link["href"], name=name, order=order, kind=kind)
        return None

    @staticmethod
    def _activity_name(activity: Tag, default: str) -> str:
        name_el = activity.select_one(".instancename")
        if not name_el:
            return default
        # The instancename often ends with a hidden " Файл"/" Тест" type label.
        text = name_el.get_text(strip=True)
        return text or default


# --------------------------------------------------------------------------- #
# Module-level DOM helpers (flexsections nesting)
# --------------------------------------------------------------------------- #
def _depth(li: Tag) -> int:
    """How many ``li.section`` ancestors a section element has (0 = top level)."""
    return len(li.find_parents("li", class_="section"))


def _child_sections(soup: BeautifulSoup, depth: int, owner: int | None = None
                    ) -> list[tuple[int, str]]:
    """Direct child subsections at ``depth`` (in document order).

    When ``owner`` is given, only children whose immediate section parent is
    ``owner`` are returned, so an unrelated tree rendered on the same page is
    ignored.
    """
    out: list[tuple[int, str]] = []
    for li in soup.select("li.section[data-number]"):
        if _depth(li) != depth:
            continue
        if owner is not None:
            parent = li.find_parent("li", class_="section")
            if parent is None or parent.get("data-number") != str(owner):
                continue
        num = li.get("data-number")
        if not num or not num.isdigit():
            continue
        name_el = li.select_one(".sectionname")
        out.append((int(num), name_el.get_text(strip=True) if name_el else f"Раздел {num}"))
    return out


def _own_activities(soup: BeautifulSoup, number: int) -> list[Tag]:
    """Activities whose nearest enclosing section is ``number``."""
    out: list[Tag] = []
    for li in soup.select("li.activity"):
        anc = li.find_parent("li", class_="section")
        if anc is not None and anc.get("data-number") == str(number):
            out.append(li)
    return out


def _module_id(activity: Tag) -> int | None:
    """Stable Moodle module id (from the ``module-NNN`` id or a view URL)."""
    el_id = activity.get("id") or ""
    m = re.match(r"module-(\d+)", el_id)
    if m:
        return int(m.group(1))
    link = activity.select_one("a[href*='/view.php?id=']")
    if link:
        m = _MODID_RE.search(link.get("href", ""))
        if m:
            return int(m.group(1))
    return None
