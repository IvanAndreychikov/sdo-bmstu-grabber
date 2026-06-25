"""Download MTS Link (webinar.ru) webinar recordings as a single composite mp4.

A webinar recording is *not* one file. The MTS Link player reconstructs it from
an event-log timeline that references several HLS streams:

  * a **conference** stream — the speaker's camera + the only audio, usually
    spanning the whole webinar;
  * one or more **screensharing** streams — the shared screen (slides / code),
    1080p and silent, shown during certain intervals.

We resolve the timeline from a public, unauthenticated API
(`gw.mts-link.ru/api/eventsessions/<id>/record`), download every stream, and
composite them into one mp4 that mirrors what you'd watch: the screen-share as
the main picture with the speaker's camera shrunk to a near-square
picture-in-picture in the top-right corner, and the camera full-frame whenever
nothing is shared. ``cuts`` (trimmed intro/outro/pauses) are honoured.

See SITE_STRUCTURE.md for the reverse-engineering notes.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .models import WebinarItem
from .moodle_client import MoodleClient
from .utils import sanitize_filename

log = logging.getLogger(__name__)

_RECORD_API = "https://gw.mts-link.ru/api/eventsessions/{esid}/record"
# The iframe src is …/record-new/<eventSessionId>; the eventSessionId is the
# number right after ``record-new/``. The path before it varies — some courses
# use ``/<org>/<eventId>/record-new/…`` and others ``/j/<org>/<eventId>/…`` — so
# we anchor on ``record-new/`` and tolerate any (quote-free) prefix.
_RECORD_URL_RE = re.compile(
    r"mts-link\.ru/[^\"'\s]*?record-new/(\d+)", re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# Timeline model
# --------------------------------------------------------------------------- #
@dataclass
class Piece:
    """A contiguous span of one source stream placed on the edited timeline.

    ``src_start``/``src_end`` index into the downloaded source file; ``ed_start``
    /``ed_end`` are positions in the final (edited) video. ``channel`` is one of
    ``camera`` (conference video), ``audio`` (conference audio) or ``screen``.
    """

    channel: str
    url: str
    src_start: float
    src_end: float
    ed_start: float
    ed_end: float
    # A still image (a shared-presentation slide) rather than a video stream:
    # it is looped for the piece's length instead of seeked into.
    is_image: bool = False

    @property
    def src_len(self) -> float:
        return self.src_end - self.src_start


@dataclass
class Timeline:
    name: str
    duration: float
    pieces: list[Piece] = field(default_factory=list)
    # url -> full source-stream duration (from the API); used to detect a
    # truncated/incomplete HLS download.
    stream_durations: dict[str, float] = field(default_factory=dict)
    # URLs that are still images (presentation slides), fetched over HTTP rather
    # than as HLS streams.
    image_sources: set[str] = field(default_factory=set)

    def urls(self) -> list[str]:
        seen: list[str] = []
        for p in self.pieces:
            if p.url not in seen:
                seen.append(p.url)
        return seen


# --------------------------------------------------------------------------- #
# Resolving + parsing
# --------------------------------------------------------------------------- #
def resolve_event_session_id(client: MoodleClient, view_url: str) -> str | None:
    """Open a Moodle ``mod/mtslink`` page and pull the MTS Link eventSessionId."""
    html = client.get(view_url).text
    m = _RECORD_URL_RE.search(html)
    if m:
        return m.group(1)
    # Fall back to scanning iframe src attributes explicitly.
    soup = BeautifulSoup(html, "lxml")
    for iframe in soup.find_all("iframe"):
        m = _RECORD_URL_RE.search(iframe.get("src") or "")
        if m:
            return m.group(1)
    return None


def fetch_record(session: requests.Session, esid: str) -> dict:
    """Fetch the record descriptor JSON (no auth needed)."""
    resp = session.get(
        _RECORD_API.format(esid=esid),
        headers={"Accept": "application/json", "Referer": "https://my.mts-link.ru/"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _cut_intervals(record: dict, t0: float) -> list[tuple[float, float]]:
    """``cuts`` as wall-clock offsets from t0 (intervals removed from the video)."""
    cuts = []
    for c in record.get("cuts") or []:
        try:
            cuts.append((c["start"] - t0, c["end"] - t0))
        except (KeyError, TypeError):
            continue
    cuts.sort()
    return cuts


def _wall_to_edited(w: float, cuts: list[tuple[float, float]]) -> float:
    """Map a wall-clock offset to its position in the edited (cut) timeline."""
    removed = 0.0
    for cs, ce in cuts:
        if ce <= w:
            removed += ce - cs
        elif cs < w < ce:
            removed += w - cs
            break
        else:
            break
    return w - removed


def _push_out_of_cut(w: float, cuts: list[tuple[float, float]]) -> float:
    """If a wall offset lands inside a removed interval, move it to the cut end."""
    for cs, ce in cuts:
        if cs <= w < ce:
            return ce
    return w


def _subtract_cuts(
    a: float, b: float, cuts: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Return the sub-intervals of wall ``[a, b]`` that survive the cuts."""
    kept = [(a, b)]
    for cs, ce in cuts:
        out: list[tuple[float, float]] = []
        for s, e in kept:
            if ce <= s or cs >= e:           # disjoint
                out.append((s, e))
                continue
            if cs > s:
                out.append((s, min(cs, e)))   # part before the cut
            if ce < e:
                out.append((max(ce, s), e))   # part after the cut
        kept = [(s, e) for s, e in out if e - s > 0.05]
    return kept


def _collect_segments(record: dict) -> list[dict]:
    """Gather every recorded media **segment**, keyed by mediasession id.

    Crucial subtlety: one logical stream (a single ``stream.id``) is recorded as
    a *sequence* of mediasession segments over time — the camera or screen is
    re-chunked every time it pauses/resumes, and **each chunk has its own
    ``hlsUrl`` and duration**. Keying by ``stream.id`` (and keeping the first
    URL) silently drops every later chunk, so a 3-hour talking head collapses to
    its first minute. We therefore key by mediasession id.

    Each segment carries: ``sid`` (stream id, for ordering chunks), ``url``,
    ``kind`` (conference/screen), ``time`` (capture epoch) and ``duration`` (from
    the matching ``mediasession.update``).
    """
    segs: dict[int, dict] = {}

    def register(entry: dict) -> None:
        mid = entry.get("id")
        url = entry.get("hlsUrl")
        stream = entry.get("stream") or {}
        sid = stream.get("id")
        if not isinstance(mid, int) or not url or not isinstance(sid, int):
            return
        kind = "screen" if "screensharing" in stream else "conference"
        seg = segs.setdefault(mid, {"sid": sid, "url": url, "kind": kind})
        if entry.get("time") is not None:
            seg.setdefault("time", entry["time"])

    def scan(obj) -> None:
        if isinstance(obj, dict):
            if "hlsUrl" in obj:
                register(obj)
            for v in obj.values():
                scan(v)
        elif isinstance(obj, list):
            for v in obj:
                scan(v)

    scan(record)

    for lg in record.get("eventLogs") or []:
        if lg.get("module") != "mediasession.update":
            continue
        data = lg.get("data")
        if not isinstance(data, dict):
            continue
        seg = segs.get(data.get("id"))
        if seg is None:
            continue
        if data.get("duration") is not None:
            seg["duration"] = data["duration"]
        if data.get("time") is not None:
            seg.setdefault("time", data["time"])

    return [s for s in segs.values() if s.get("time") is not None]


def _collect_slides(record: dict) -> list[tuple[float, str | None]]:
    """Reconstruct a shared **presentation** as ``(edited_time, slide_url)`` marks.

    Some webinars don't share a screen *video* at all — the presenter uploads a
    PDF/PPTX, which MTS Link converts to per-slide JPEGs and shows client-side.
    Each ``presentation.update`` event names the slide on screen at that moment
    (``fileReference.slide.url``) and carries ``relativeTime`` (already a position
    in the edited timeline). A slide stays up until the next mark.

    ``isActive`` matters: the presenter can hide the deck (``isActive=False``) and
    show only the camera, then bring it back later. A hidden update is recorded as
    a ``None`` mark, which ends the current slide and leaves a gap until the next
    slide appears. Runs of the same state are collapsed so we don't split the
    video needlessly.
    """
    marks: list[tuple[float, str | None]] = []
    rows = []
    for lg in record.get("eventLogs") or []:
        if lg.get("module") != "presentation.update":
            continue
        rt = lg.get("relativeTime")
        data = lg.get("data")
        if rt is None or not isinstance(data, dict):
            continue
        slide = (data.get("fileReference") or {}).get("slide") or {}
        url = slide.get("url") if data.get("isActive") else None
        rows.append((float(rt), url))
    rows.sort(key=lambda m: m[0])
    for rt, url in rows:
        if marks and marks[-1][1] == url:
            continue                       # same slide / still hidden
        marks.append((rt, url))
    return marks


def build_timeline(record: dict) -> Timeline:
    """Turn a record descriptor into an ordered list of timeline pieces."""
    duration = float(record.get("duration") or 0.0)
    logs = record.get("eventLogs") or []
    t0 = logs[0]["time"] if logs else 0.0
    cuts = _cut_intervals(record, t0)

    # Group segments by stream id and order by capture time so each chunk's
    # extent can be capped at the next chunk's start (chunks are back-to-back;
    # the duration limits a chunk when the stream then went silent for a while).
    by_sid: dict[int, list[dict]] = {}
    for seg in _collect_segments(record):
        by_sid.setdefault(seg["sid"], []).append(seg)

    pieces: list[Piece] = []
    known_dur: dict[str, float] = {}
    for group in by_sid.values():
        group.sort(key=lambda s: s["time"])
        for i, seg in enumerate(group):
            ws_raw = seg["time"] - t0
            ext = float(seg["duration"]) if seg.get("duration") is not None else duration
            if i + 1 < len(group):                      # never run into the next chunk
                ext = min(ext, (group[i + 1]["time"] - t0) - ws_raw)
            if seg.get("duration") is not None:
                known_dur[seg["url"]] = float(seg["duration"])
            shown_start = _push_out_of_cut(ws_raw, cuts)
            wall_end = ws_raw + ext
            if wall_end <= shown_start:
                continue
            for a, b in _subtract_cuts(shown_start, wall_end, cuts):
                piece = Piece(
                    channel="screen" if seg["kind"] == "screen" else "camera",
                    url=seg["url"],
                    src_start=max(0.0, a - ws_raw),
                    src_end=b - ws_raw,
                    ed_start=_wall_to_edited(a, cuts),
                    ed_end=_wall_to_edited(b, cuts),
                )
                if seg["kind"] == "screen":
                    pieces.append(piece)
                else:
                    # A conference chunk feeds both the camera video and the audio.
                    pieces.append(piece)
                    pieces.append(Piece("audio", seg["url"], piece.src_start,
                                        piece.src_end, piece.ed_start, piece.ed_end))

    # Shared-presentation slides fill in as the "screen" wherever no real
    # screensharing *video* is active (so webinars that only ever showed an
    # uploaded deck still get a screen, and ones that did screen-share keep the
    # video untouched). Slide marks are already in edited-timeline seconds.
    image_sources: set[str] = set()
    screen_cov = sorted((p.ed_start, p.ed_end) for p in pieces if p.channel == "screen")
    slides = _collect_slides(record)
    for i, (start, url) in enumerate(slides):
        if url is None:                    # presenter hid the deck → no slide
            continue
        end = slides[i + 1][0] if i + 1 < len(slides) else duration
        start = max(0.0, start)
        end = min(duration, end)
        if end - start <= 0.1:
            continue
        for a, b in _subtract_cuts(start, end, screen_cov):
            pieces.append(Piece("screen", url, 0.0, b - a, a, b, is_image=True))
            image_sources.add(url)

    pieces.sort(key=lambda p: (p.ed_start, p.channel))
    return Timeline(name=str(record.get("name") or "webinar"),
                    duration=duration, pieces=pieces, stream_durations=known_dur,
                    image_sources=image_sources)


def _edited_to_wall(ed: float, cuts: list[tuple[float, float]]) -> float:
    """Inverse of :func:`_wall_to_edited` (cuts are added back in)."""
    w = ed
    for cs, ce in cuts:
        if cs <= w:
            w += ce - cs
    return w


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
_VID_RE = re.compile(r"Stream #\d+:\d+.*Video:.*?(\d{2,5})x(\d{2,5})")
_AUD_RE = re.compile(r"Stream #\d+:\d+.*Audio:")


@dataclass
class Probe:
    has_video: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0
    duration: float = 0.0


# Run heavy ffmpeg work at below-normal priority so the whole OS stays
# responsive (a full-speed encode can otherwise starve the desktop). On POSIX we
# prepend `nice` rather than use ``preexec_fn``: the latter is unsafe in a
# multithreaded process (we render webinar segments on a thread pool) and the
# CPython docs warn it can deadlock. `nice` is part of coreutils and is present
# on every Linux/macOS box; if it somehow isn't, we just run at normal priority.
if os.name == "nt":
    _PRIORITY_PREFIX: list[str] = []
    _PRIORITY_KWARGS: dict = {"creationflags": 0x00004000}  # BELOW_NORMAL_PRIORITY
else:
    _nice = shutil.which("nice")
    _PRIORITY_PREFIX = [_nice, "-n", "10"] if _nice else []
    _PRIORITY_KWARGS = {}


def _run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess (captured, utf-8) at below-normal priority.

    ``timeout`` is essential: an ffmpeg call can wedge (e.g. seeking past the end
    of a stream produced no frames), and without a bound it would hang forever.
    On timeout the process is killed and a failed result is returned.
    """
    try:
        return subprocess.run(_PRIORITY_PREFIX + cmd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, **_PRIORITY_KWARGS)
    except subprocess.TimeoutExpired:
        log.warning("    ffmpeg timed out after %.0fs — killed", timeout or 0)
        return subprocess.CompletedProcess(cmd, -9, "", f"timeout after {timeout}s")


def _cpu_thread_limit(fraction: float) -> int:
    """Cap worker threads so ffmpeg never grabs the whole machine."""
    cores = os.cpu_count() or 4
    return max(1, min(cores, int(round(cores * fraction))))


def _probe(ffmpeg: str, path: Path) -> Probe:
    out = _run([ffmpeg, "-hide_banner", "-i", str(path)]).stderr
    pr = Probe(has_audio=bool(_AUD_RE.search(out)))
    m = _VID_RE.search(out)
    if m:
        pr.has_video = True
        pr.width, pr.height = int(m.group(1)), int(m.group(2))
    m = _DUR_RE.search(out)
    if m:
        pr.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    return pr


def _nvenc_works(ffmpeg: str) -> bool:
    """True only if NVENC is both compiled in *and* usable at runtime.

    ffmpeg lists ``h264_nvenc`` even on machines with no NVIDIA GPU/driver, so a
    tiny real encode is the only reliable probe.
    """
    encoders = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
    if "h264_nvenc" not in encoders.stdout:
        return False
    # 320x240: NVENC rejects frames below its minimum dimensions, so the test
    # clip must be comfortably above that or it fails even on a working GPU. The
    # timeout guards against a wedged/broken driver hanging startup.
    test = _run([ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
                 "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=30)
    return test.returncode == 0


def _video_encoder(ffmpeg: str, prefer: str, fps: int) -> tuple[list[str], str]:
    """Return ``(encoder args, name)``.

    ``prefer`` is ``auto`` | ``nvenc`` | ``cpu``. ``auto`` uses NVENC only when it
    actually works (NVIDIA GPU present), otherwise everything runs on CPU via
    libx264. A regular keyframe interval (~2 s) is forced so seeking stays exact.
    """
    gop = max(2, fps * 2)
    use_nvenc = prefer == "nvenc" or (prefer != "cpu" and _nvenc_works(ffmpeg))
    if use_nvenc:
        return (["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
                 "-cq", "26", "-b:v", "0", "-g", str(gop), "-bf", "0",
                 "-pix_fmt", "yuv420p"], "h264_nvenc (GPU)")
    return (["-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
             "-g", str(gop), "-keyint_min", str(fps),
             "-pix_fmt", "yuv420p"], "libx264 (CPU)")


# --------------------------------------------------------------------------- #
# Downloader / compositor
# --------------------------------------------------------------------------- #
class WebinarDownloader:
    """Resolve, download and composite an MTS Link webinar into one mp4."""

    def __init__(
        self,
        client: MoodleClient,
        ffmpeg: str,
        skip_existing: bool = True,
        width: int = 1920,
        height: int = 1080,
        fps: int = 25,
        encoder: str = "auto",
        cpu_fraction: float = 0.75,
    ):
        self.client = client
        self.ffmpeg = ffmpeg
        self.skip_existing = skip_existing
        self.W, self.H = width, height
        self.fps = fps
        self.pip = max(160, height // 4)         # near-square PiP side
        self.margin = max(12, height // 45)
        cores = os.cpu_count() or 4
        self.threads = _cpu_thread_limit(cpu_fraction)
        self._encoder, name = _video_encoder(ffmpeg, encoder, fps)
        is_gpu = any("nvenc" in a for a in self._encoder)
        # Segments and downloads are rendered/fetched concurrently to keep the
        # GPU/CPU and network busy instead of idling during each ffmpeg's
        # startup + seek. We split the SAME thread budget between the parallel
        # segment encoders, so total CPU (and therefore RAM) stays within the
        # cpu_fraction cap regardless of how many run at once. NVENC is capped at
        # 2 concurrent sessions — consumer drivers limit simultaneous encodes,
        # and a refused session would degrade a segment to a black filler.
        self.seg_workers = max(1, min(2 if is_gpu else 3, cores // 4))
        self.seg_threads = max(1, self.threads // self.seg_workers)
        # Stream remuxing is `-c copy` — network-bound and CPU-light — so more
        # of them can overlap than there are cores.
        self.dl_workers = max(1, min(6, cores // 2))
        self.dl_threads = max(1, min(2, self.threads // self.dl_workers))
        log.info(
            "Webinar compositor: %s, %dx%d, ≤%d threads (~%.0f%% of CPU), "
            "%d×%d-thread segment encoders, %d parallel downloads, "
            "below-normal priority",
            name, self.W, self.H, self.threads, cpu_fraction * 100,
            self.seg_workers, self.seg_threads, self.dl_workers,
        )

    # -- public ----------------------------------------------------------------
    def download(self, item: WebinarItem, dest_dir: Path) -> Path | None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{sanitize_filename(f'{item.order:02d} - {item.name}')}.mp4"
        if dest.exists() and self.skip_existing:
            log.info("  ✓ webinar already downloaded: %s", dest.name)
            return dest

        esid = resolve_event_session_id(self.client, item.url)
        if esid is None:
            log.warning("  ✗ no MTS Link recording found for: %s", item.name)
            return None
        try:
            record = fetch_record(self.client.session, esid)
        except requests.RequestException as exc:
            log.warning("  ✗ record API failed for %s: %s", item.name, exc)
            return None

        timeline = build_timeline(record)
        if not timeline.pieces:
            log.warning("  ✗ empty webinar timeline: %s", item.name)
            return None
        n_streams = len(timeline.urls())
        log.info("  • webinar '%s': %.0f min, %d streams",
                 item.name, timeline.duration / 60, n_streams)

        with tempfile.TemporaryDirectory(prefix="webinar_") as tmp:
            log.info("    ↓ downloading %d webinar stream(s)…", n_streams)
            sources = self._download_streams(timeline, Path(tmp))
            if sources is None:
                return None
            log.info("    ⚙ streams downloaded — starting LOCAL compositing "
                     "(CPU/GPU-intensive, ~%.0f min of video to encode)…",
                     timeline.duration / 60)
            ok = self._render_verified(timeline, sources, dest)
            log.info("    ⚙ local compositing finished")
        if not ok:
            log.warning("  ✗ webinar could not be produced cleanly: %s", item.name)
            return None
        log.info("  ✓ saved webinar: %s", dest.name)
        return dest

    # -- stream download -------------------------------------------------------
    def _download_streams(
        self, timeline: Timeline, tmp: Path
    ) -> dict[str, dict] | None:
        """Download each HLS stream to a local mp4, verifying completeness.

        Streams are fetched concurrently (network-bound `-c copy` remuxes), which
        turns N sequential network waits into a handful of overlapping ones — the
        single biggest win for a webinar with dozens of chunks.
        """
        urls = timeline.urls()

        def fetch(item: tuple[int, str]) -> tuple[str, Path, Probe | None]:
            i, url = item
            if url in timeline.image_sources:
                local = tmp / f"slide_{i}.jpg"
                return url, local, self._fetch_image(url, local)
            local = tmp / f"src_{i}.mp4"
            expected = timeline.stream_durations.get(url, 0.0)
            return url, local, self._fetch_hls(url, local, expected)

        sources: dict[str, dict] = {}
        workers = max(1, min(self.dl_workers, len(urls)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for url, local, probe in ex.map(fetch, enumerate(urls)):
                if probe is None:
                    log.warning("  ✗ failed to download a webinar stream: %s", local.name)
                    return None
                sources[url] = {"path": local, "probe": probe}
        return sources

    def _fetch_hls(self, url: str, dest: Path, expected: float) -> Probe | None:
        """Download one HLS stream to a local mp4.

        A clean ffmpeg exit means the whole playlist was read; we retry only on a
        real download error (non-zero exit or an unreadable file). We do *not*
        fail just because the result is shorter than the API's declared duration:
        that metadata is sometimes optimistic, the stream genuinely is that long,
        and the compositor already clamps each piece to the real footage. (A
        corrupt tail is still caught later by the output integrity check.)
        """
        for attempt in range(1, 4):
            rc = _run(
                [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-threads", str(self.dl_threads),
                 "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", str(dest)],
                timeout=2400,
            ).returncode
            if rc == 0 and dest.exists() and dest.stat().st_size > 1024:
                probe = _probe(self.ffmpeg, dest)
                if expected > 0 and probe.duration < expected * 0.9 - 1:
                    log.info("    • stream shorter than API metadata "
                             "(%.0f/%.0fs) — using what was recorded",
                             probe.duration, expected)
                return probe
            log.info("    … stream download retry %d/3", attempt)
        return None

    def _fetch_image(self, url: str, dest: Path) -> Probe | None:
        """Download one presentation-slide image over HTTP and probe its size."""
        for attempt in range(1, 4):
            try:
                r = self.client.session.get(
                    url, timeout=120,
                    headers={"Referer": "https://my.mts-link.ru/"},
                )
                r.raise_for_status()
                dest.write_bytes(r.content)
            except requests.RequestException as exc:
                log.info("    … slide download retry %d/3 (%s)", attempt, exc)
                continue
            if dest.exists() and dest.stat().st_size > 256:
                return _probe(self.ffmpeg, dest)
        return None

    # -- compositing -----------------------------------------------------------
    def _render_verified(
        self, timeline: Timeline, sources: dict[str, dict], dest: Path
    ) -> bool:
        """Compose to a temp file, verify it, and only then publish to ``dest``.

        Writing to a ``.part.mp4`` first means a crash or a bad encode never
        leaves a broken file under the final name (which ``skip_existing`` would
        otherwise treat as done). A failed/corrupt result is retried once.
        """
        tmp_out = dest.with_name(dest.stem + ".part.mp4")
        for attempt in range(1, 3):
            ok = self._compose(timeline, sources, tmp_out)
            if not ok:
                log.warning("  ✗ compose failed (try %d/2)", attempt)
            elif not self._verify_output(tmp_out, timeline.duration):
                log.warning("  ✗ composed file failed integrity check "
                            "(try %d/2) — redoing", attempt)
            else:
                tmp_out.replace(dest)
                return True
            tmp_out.unlink(missing_ok=True)
        return False

    def _compose(
        self, timeline: Timeline, sources: dict[str, dict], out_path: Path
    ) -> bool:
        """Memory-bounded compositor.

        Instead of one giant ``filter_complex`` that opens every stream and shifts
        clips across the whole timeline (which makes ffmpeg buffer huge amounts of
        frames → RAM blow-up), we:

          1. build the continuous audio track in one light, audio-only pass;
          2. render the video as short, independent **segments** split at every
             layout change — each opens only the 1-2 streams active then, with
             input-level seeking so only that span is decoded;
          3. concatenate the segments and mux in the audio.

        At no point are more than a couple of streams (or more than one segment's
        worth of frames) held in memory, regardless of webinar length.
        """
        cam, screen, audio = self._classify_pieces(timeline, sources)
        with tempfile.TemporaryDirectory(prefix="wcompose_") as td:
            tmp = Path(td)
            audio_path = tmp / "audio.m4a"
            video_path = tmp / "video.mp4"
            if not self._build_audio(timeline, sources, audio, audio_path):
                return False
            if not self._build_video(timeline, sources, cam, screen, tmp, video_path):
                return False
            mux = _run([
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(video_path), "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                "-movflags", "+faststart", str(out_path),
            ], timeout=max(300.0, timeline.duration * 0.2))
            if mux.returncode != 0:
                log.warning("  ✗ mux failed: %s", mux.stderr[-400:])
                return False
        return True

    def _classify_pieces(
        self, timeline: Timeline, sources: dict[str, dict]
    ) -> tuple[list[Piece], list[Piece], list[Piece]]:
        """Split pieces into camera / screen / audio, dropping channels the source
        lacks and clamping each to the stream's real (probed) length so ffmpeg
        never reads past end-of-file (the API's per-stream duration can be
        optimistic)."""
        cam, screen, audio = [], [], []
        for p in timeline.pieces:
            pr: Probe = sources[p.url]["probe"]
            if pr.duration > 0 and pr.duration - p.src_start <= 0.05:
                continue  # nothing of this piece was actually recorded
            if pr.duration > 0 and p.src_end > pr.duration:
                p = replace(p, src_end=pr.duration,
                            ed_end=p.ed_start + (pr.duration - p.src_start))
            if p.channel == "camera" and pr.has_video:
                cam.append(p)
            elif p.channel == "screen" and pr.has_video:
                screen.append(p)
            elif p.channel == "audio" and pr.has_audio:
                audio.append(p)
        return cam, screen, audio

    def _build_audio(
        self, timeline: Timeline, sources: dict[str, dict],
        audio: list[Piece], out_path: Path,
    ) -> bool:
        """One light, audio-only pass → a continuous track spanning the whole
        webinar (silence where nobody was speaking)."""
        files = list({p.url: sources[p.url]["path"] for p in audio}.items())
        in_index = {url: i for i, (url, _) in enumerate(files)}
        # A full-length silent base guarantees the track always spans [0, dur].
        chains = [f"anullsrc=r=48000:cl=stereo,atrim=0:{timeline.duration:.3f}[abase]"]
        labels = ["abase"]
        for k, p in enumerate(audio):
            chains.append(
                f"[{in_index[p.url]}:a]atrim={p.src_start:.3f}:{p.src_end:.3f},"
                f"asetpts=PTS-STARTPTS,adelay={int(p.ed_start * 1000)}:all=1[a{k}]"
            )
            labels.append(f"a{k}")
        mix = "".join(f"[{l}]" for l in labels)
        chains.append(
            f"{mix}amix=inputs={len(labels)}:normalize=0:dropout_transition=0[aout]"
        )
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-threads", str(self.threads)]
        for _, path in files:
            cmd += ["-i", str(path)]
        cmd += ["-filter_complex", ";".join(chains), "-map", "[aout]",
                "-c:a", "aac", "-b:a", "160k", "-t", f"{timeline.duration:.3f}",
                str(out_path)]
        result = _run(cmd, timeout=max(600.0, timeline.duration))
        if result.returncode != 0:
            log.warning("  ✗ audio build failed: %s", result.stderr[-400:])
            return False
        return True

    def _build_video(
        self, timeline: Timeline, sources: dict[str, dict],
        cam: list[Piece], screen: list[Piece], tmp: Path, out_path: Path,
    ) -> bool:
        """Render the video as independent segments and concatenate them."""
        # Boundaries = every point where the active camera/screen piece changes.
        marks = {0.0, timeline.duration}
        for p in cam + screen:
            marks.add(max(0.0, p.ed_start))
            marks.add(min(timeline.duration, p.ed_end))
        bounds = sorted(m for m in marks if 0.0 <= m <= timeline.duration)
        intervals = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)
                     if bounds[i + 1] - bounds[i] >= 0.1]

        seg_files = [tmp / f"seg_{i:04d}.mp4" for i in range(len(intervals))]

        def render(i: int) -> tuple[int, bool]:
            a, b = intervals[i]
            return i, self._make_segment(a, b, cam, screen, sources, seg_files[i])

        # Encode several segments at once. Each ffmpeg holds only its own short
        # interval's frames, so a few in flight stays memory-bounded; together
        # they keep the encoder saturated instead of stalling on per-process
        # startup and input seeking between segments.
        workers = max(1, min(self.seg_workers, len(intervals)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, ok in ex.map(render, range(len(intervals))):
                if not ok:
                    log.warning("  ✗ segment %d/%d unrecoverable", i, len(intervals))
                    return False

        if not seg_files:
            return False
        listing = tmp / "segments.txt"
        listing.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in seg_files), encoding="utf-8"
        )
        concat = _run([
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(out_path),
        ], timeout=max(300.0, timeline.duration * 0.2))
        if concat.returncode != 0:
            log.warning("  ✗ concat failed: %s", concat.stderr[-400:])
            return False
        return True

    def _make_segment(
        self, a: float, b: float, cam: list[Piece], screen: list[Piece],
        sources: dict[str, dict], seg: Path,
    ) -> bool:
        """Render one interval, validate it, and fall back to black on trouble.

        A segment that fails, hangs (→ timeout) or comes out empty/short would
        otherwise wreck the whole webinar. Replacing it with a black clip of the
        exact interval length keeps the timeline aligned and lets the rest play.
        """
        length = b - a
        budget = max(180.0, length * 4)
        cmd = self._segment_cmd(a, b, cam, screen, sources, seg)
        result = _run(cmd, timeout=budget)
        if result.returncode == 0 and self._segment_ok(seg, length):
            return True
        log.info("    • segment %.0f–%.0fs unusable — black filler", a, b)
        black = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-t", f"{length:.3f}",
                 "-i", f"color=c=black:s={self.W}x{self.H}:r={self.fps}",
                 *self._encoder, "-an", "-fps_mode", "cfr", "-r", str(self.fps),
                 "-t", f"{length:.3f}", str(seg)]
        return _run(black, timeout=max(120.0, length)).returncode == 0 \
            and self._segment_ok(seg, length)

    def _segment_ok(self, seg: Path, length: float) -> bool:
        """A usable segment has a video stream and roughly the right length."""
        if not seg.exists() or seg.stat().st_size < 256:
            return False
        pr = _probe(self.ffmpeg, seg)
        return pr.has_video and pr.duration >= min(length, 1.0) * 0.5

    def _segment_cmd(
        self, a: float, b: float, cam: list[Piece], screen: list[Piece],
        sources: dict[str, dict], seg: Path,
    ) -> list[str]:
        """Build the ffmpeg command for one constant-layout interval ``[a, b]``.

        A black canvas of exactly ``length`` seconds is the base, so the segment
        is always full-length even if a source runs out early; the screen and/or
        camera are overlaid on top (``eof_action=pass`` → revert to black when a
        source ends). Layout: screen as the main picture + camera as a top-right
        near-square PiP; or camera full-frame; or black. Audio is added later.
        """
        length = b - a
        cam_p = _covering(cam, a, b)
        scr_p = _covering(screen, a, b)

        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-threads", str(self.seg_threads),
               "-f", "lavfi", "-t", f"{length:.3f}",
               "-i", f"color=c=black:s={self.W}x{self.H}:r={self.fps}"]
        chains: list[str] = []
        n = 1  # input 0 is the black base

        def add_input(piece: Piece) -> int:
            nonlocal n
            if piece.is_image:
                # A static slide: loop the single frame for the whole interval
                # instead of seeking into a (non-existent) video timeline.
                cmd.extend(["-loop", "1", "-t", f"{length:.3f}",
                            "-i", str(sources[piece.url]["path"])])
            else:
                src = piece.src_start + (a - piece.ed_start)
                cmd.extend(["-ss", f"{max(0.0, src):.3f}", "-t", f"{length:.3f}",
                            "-i", str(sources[piece.url]["path"])])
            i = n
            n += 1
            return i

        fit = (f"scale={self.W}:{self.H}:force_original_aspect_ratio=decrease,"
               f"pad={self.W}:{self.H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
        ov = "eof_action=pass"
        chains.append("[0:v]setsar=1[bg]")
        if scr_p is not None:
            i_scr = add_input(scr_p)
            chains.append(f"[{i_scr}:v]{fit},fps={self.fps}[scr]")
            chains.append(f"[bg][scr]overlay=0:0:{ov}[bg1]")
            main = "bg1"
        else:
            main = "bg"
        if cam_p is not None:
            i_cam = add_input(cam_p)
            if scr_p is not None:
                # camera shrunk to a near-square PiP in the top-right corner
                chains.append(f"[{i_cam}:v]crop='min(iw,ih)':'min(iw,ih)',"
                              f"scale={self.pip}:{self.pip},setsar=1,fps={self.fps}[cam]")
                chains.append(
                    f"[{main}][cam]overlay=W-w-{self.margin}:{self.margin}:{ov}[v]")
            else:
                chains.append(f"[{i_cam}:v]{fit},fps={self.fps}[cam]")
                chains.append(f"[{main}][cam]overlay=0:0:{ov}[v]")
        else:
            chains.append(f"[{main}]null[v]")

        cmd += ["-filter_complex", ";".join(chains), "-map", "[v]", "-an",
                *self._encoder, "-threads", str(self.seg_threads),
                "-fps_mode", "cfr", "-r", str(self.fps),
                "-t", f"{length:.3f}", str(seg)]
        return cmd

    def _verify_output(self, path: Path, expected: float) -> bool:
        """Check a finished webinar is complete and seekable.

        Catches the two failures seen in practice: a truncated/never-opening
        file, and one that breaks when you scrub. We confirm both streams exist,
        the duration matches, the tail decodes (no truncation) and a mid-file
        seek decodes (index/keyframes are sound).
        """
        if not path.exists() or path.stat().st_size < 1024:
            return False
        pr = _probe(self.ffmpeg, path)
        if not (pr.has_video and pr.has_audio):
            return False
        if expected > 0 and abs(pr.duration - expected) > max(3.0, expected * 0.03):
            log.warning("    duration mismatch: got %.0fs, expected %.0fs",
                        pr.duration, expected)
            return False
        tail = _run([self.ffmpeg, "-v", "error", "-sseof", "-2",
                     "-i", str(path), "-f", "null", "-"], timeout=180)
        if tail.returncode != 0 or tail.stderr.strip():
            return False
        seek = _run([self.ffmpeg, "-v", "error", "-ss", f"{expected * 0.6:.1f}",
                     "-i", str(path), "-t", "1", "-f", "null", "-"], timeout=180)
        return seek.returncode == 0 and not seek.stderr.strip()

def _covering(pieces: list[Piece], a: float, b: float) -> Piece | None:
    """The piece (if any) whose edited span fully contains interval ``[a, b]``.

    Interval boundaries are built from every piece edge, so within an interval a
    piece is either fully active or fully inactive — a small epsilon absorbs
    floating-point noise at the edges."""
    eps = 0.01
    for p in pieces:
        if p.ed_start <= a + eps and p.ed_end >= b - eps:
            return p
    return None
