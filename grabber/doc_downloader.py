"""Save non-file modules (pages, quizzes, feedback, assignments) as documents.

Moodle ``mod/page`` text lessons and the view pages of quizzes / feedback /
assignments aren't downloadable files, but they still hold material. We capture
the visible content as a PDF (falling back to a self-contained HTML file if the
PDF toolchain isn't installed). Only the already-visible view page is fetched —
no quiz attempt or submission is ever started, so the user's grades and attempt
counts are untouched.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import DocItem
from .moodle_client import MoodleClient
from .utils import sanitize_filename

log = logging.getLogger(__name__)

# Candidate (regular, bold) TrueType fonts that cover Cyrillic. xhtml2pdf's
# built-in Helvetica has no Cyrillic glyphs (Russian text renders as boxes), so a
# real font must be registered via @font-face. First existing pair wins.
# Preferred (regular, bold) Cyrillic-capable TrueType filenames, in priority
# order. We search for these by *name* across the platform's font directories,
# because the exact path varies (Linux distros place DejaVu differently; macOS
# moved Arial to .../Supplemental). DejaVu is near-universal on Linux, Arial on
# macOS/Windows; Liberation/Noto/FreeSans are common fallbacks.
_FONT_NAMES = [
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("Arial.ttf", "Arial Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"),
    ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"),
    ("FreeSans.ttf", "FreeSansBold.ttf"),
    ("segoeui.ttf", "seguisb.ttf"),
]

# Standard font directories per platform (only the existing ones are searched).
_FONT_DIRS = [
    "/usr/share/fonts", "/usr/local/share/fonts",                  # Linux
    str(Path.home() / ".fonts"), str(Path.home() / ".local/share/fonts"),
    "/Library/Fonts", "/System/Library/Fonts",                    # macOS
    "/System/Library/Fonts/Supplemental", str(Path.home() / "Library/Fonts"),
    r"C:\Windows\Fonts",                                          # Windows
]


def _font_index() -> dict[str, str]:
    """Map ``basename -> full path`` for every .ttf under the font dirs (once)."""
    index: dict[str, str] = {}
    for d in _FONT_DIRS:
        root = Path(d)
        if not root.is_dir():
            continue
        try:
            for f in root.rglob("*.ttf"):
                index.setdefault(f.name, str(f))
        except OSError:
            continue
    return index


def _find_fonts() -> tuple[str | None, str | None]:
    index = _font_index()
    for regular, bold in _FONT_NAMES:
        if regular in index:
            return index[regular], index.get(bold)
    return None, None


_FONT_FAMILY: str | None = None
_FONT_DONE = False


def _register_font() -> str | None:
    """Register a Cyrillic TTF with reportlab + xhtml2pdf, once per process.

    ``@font-face`` is avoided on purpose: xhtml2pdf copies the font to a temp
    file that stays locked on Windows, so it fails there. Registering the font
    with reportlab and adding it to xhtml2pdf's ``DEFAULT_FONT`` map makes a plain
    ``font-family`` reference work on every platform. Returns the family name to
    use, or ``None`` if no suitable font was found.
    """
    global _FONT_FAMILY, _FONT_DONE
    if _FONT_DONE:
        return _FONT_FAMILY
    _FONT_DONE = True
    regular, bold = _find_fonts()
    if not regular:
        log.warning("no Cyrillic TTF font found — PDF text may render as boxes")
        return None
    try:
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        import xhtml2pdf.default as default

        pdfmetrics.registerFont(TTFont("docfont", regular))
        addMapping("docfont", 0, 0, "docfont")
        if bold:
            pdfmetrics.registerFont(TTFont("docfont-b", bold))
            addMapping("docfont", 1, 0, "docfont-b")
        default.DEFAULT_FONT["docfont"] = "docfont"
        _FONT_FAMILY = "docfont"
    except Exception as exc:
        log.warning("could not register PDF font: %s", exc)
        _FONT_FAMILY = None
    return _FONT_FAMILY


def _style(family: str) -> str:
    return f"""
@page {{ size: A4; margin: 1.6cm; }}
body {{ font-family: {family}; font-size: 11pt; line-height: 1.45; color: #111; }}
h1, h2, h3 {{ color: #1a3c6e; font-family: {family}; }}
img {{ max-width: 100%; }}
table {{ border-collapse: collapse; table-layout: fixed; width: 100%;
        word-wrap: break-word; }}
td, th {{ border: 1px solid #999; padding: 3px 6px; font-family: {family}; }}
a {{ word-wrap: break-word; }}
.src {{ color: #888; font-size: 8pt; }}
"""


class DocDownloader:
    """Fetch a module's view page and store its content as PDF (or HTML)."""

    def __init__(self, client: MoodleClient, skip_existing: bool = True):
        self.client = client
        self.skip_existing = skip_existing
        self._pisa = _load_pisa()
        family = _register_font() if self._pisa else None
        self._style = _style(family or "Arial, sans-serif")

    def download(self, doc: DocItem, dest_dir: Path) -> Path | None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = sanitize_filename(f"{doc.order:02d} - {doc.name}")
        ext = "pdf" if self._pisa else "html"
        dest = dest_dir / f"{stem}.{ext}"
        # If either representation already exists, treat as done.
        for cand in (dest_dir / f"{stem}.pdf", dest_dir / f"{stem}.html"):
            if cand.exists() and self.skip_existing:
                log.info("  ✓ document already saved: %s", cand.name)
                return cand

        try:
            page = self.client.get_soup(doc.url)
        except Exception as exc:
            log.warning("  ✗ could not open %s '%s': %s", doc.kind, doc.name, exc)
            return None

        html = self._build_html(page, doc)
        if self._pisa is not None:
            if self._render_pdf(html, doc.url, dest):
                log.info("  ✓ saved %s: %s", doc.kind, dest.name)
                return dest
            log.warning("    PDF render failed — falling back to HTML")
            dest = dest_dir / f"{stem}.html"
        dest.write_text(html, encoding="utf-8")
        log.info("  ✓ saved %s: %s", doc.kind, dest.name)
        return dest

    # -- content extraction ----------------------------------------------------
    def _build_html(self, page: BeautifulSoup, doc: DocItem) -> str:
        main = (page.select_one("[role='main']") or page.select_one("#region-main")
                or page.body or page)
        # Drop chrome that shouldn't be in the document.
        for sel in ("script", "style", "nav", "form", "iframe",
                    ".navbar", ".secondary-navigation", ".activity-navigation",
                    "[data-region='blocks-column']", ".addblockbutton",
                    ".modal", ".visually-hidden", ".accesshide"):
            for el in main.select(sel):
                el.extract()
        # Resolve relative links/images to absolute so the link_callback can fetch.
        base = doc.url
        for tag, attr in (("img", "src"), ("a", "href")):
            for el in main.find_all(tag):
                if el.get(attr):
                    el[attr] = urljoin(base, el[attr])
        title = sanitize_filename(doc.name)
        body = main.decode_contents()
        return (
            "<html><head><meta charset='utf-8'>"
            f"<style>{self._style}</style></head><body>"
            f"<h1>{title}</h1>{body}"
            f"<p class='src'>Источник: {doc.url}</p>"
            "</body></html>"
        )

    def _render_pdf(self, html: str, base_url: str, dest: Path) -> bool:
        """Render HTML→PDF, fetching images over the authenticated session."""
        tmpdir = Path(tempfile.mkdtemp(prefix="moodledoc_"))
        downloaded: list[Path] = []

        def link_callback(uri: str, _rel: str) -> str:
            if uri.startswith(("http://", "https://")):
                try:
                    r = self.client.session.get(uri, timeout=60)
                    r.raise_for_status()
                    suffix = Path(uri.split("?")[0]).suffix or ".img"
                    f = tmpdir / f"img_{len(downloaded)}{suffix}"
                    f.write_bytes(r.content)
                    downloaded.append(f)
                    return str(f)
                except Exception:
                    return uri  # let pisa skip a broken image
            return uri

        try:
            with dest.open("wb") as fh:
                result = self._pisa.CreatePDF(
                    html, dest=fh, link_callback=link_callback, encoding="utf-8"
                )
            ok = not result.err and dest.exists() and dest.stat().st_size > 512
        except Exception as exc:
            log.warning("    pisa error: %s", exc)
            ok = False
        finally:
            for f in downloaded:
                f.unlink(missing_ok=True)
            try:
                tmpdir.rmdir()
            except OSError:
                pass
        if not ok:
            dest.unlink(missing_ok=True)
        return ok


def _load_pisa():
    """Import xhtml2pdf's pisa lazily; ``None`` if it isn't installed."""
    try:
        from xhtml2pdf import pisa
        return pisa
    except Exception:
        log.warning("xhtml2pdf not installed — documents will be saved as HTML "
                    "(run: pip install xhtml2pdf)")
        return None
