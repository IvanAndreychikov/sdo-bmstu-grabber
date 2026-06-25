# sdo-bmstu-grabber — architecture

Purpose: log in to the BMSTU distance-learning system (Moodle), walk the course
sections **recursively** (including arbitrarily deep nested subsections), and
save to disk the **videos (highest quality)**, **webinars** (MTS Link recordings,
composited locally), **presentations**, **linked files** (e.g. code notebooks),
and **text pages / quizzes / feedback** (captured as PDF) — preserving the video
titles and mirroring the full course structure as nested folders.

## Stack and dependencies

- **Python 3.13**, isolated `venv/` in the project root.
- `requests` + `beautifulsoup4` + `lxml` — Moodle login and HTML parsing.
- `yt-dlp` — rutube downloads (understands private embed URLs, picks the best
  quality itself, merges HLS).
- `imageio-ffmpeg` — ships a static `ffmpeg` binary right inside the venv (no
  admin rights), needed by yt-dlp to remux into mp4.
- `gdown` — downloads files that URL modules link to on Google Drive.
- `imageio-ffmpeg`'s bundled ffmpeg also downloads & composites **MTS Link
  webinars** (HLS → one picture-in-picture mp4); NVENC (`h264_nvenc`) is used
  automatically when an NVIDIA GPU is present, else libx264.

## Module layout

```
sdo-bmstu-grabber/
├── venv/                     # virtual environment
├── result/                   # DOWNLOADS: course tree (NOT in git)
├── config.json               # credentials and settings (NOT in git)
├── config.example.json       # settings template
├── requirements.txt
├── main.py                   # CLI entry point
├── SITE_STRUCTURE.md         # analysis of the target site
├── ARCHITECTURE.md           # this file
├── README.md                 # description (EN)
├── README.ru.md              # description (RU)
└── grabber/                  # package
    ├── __init__.py
    ├── config.py             # Config: defaults < config.json < env < CLI
    ├── models.py             # Section / SectionNode / VideoItem / FileItem /
    │                         #   WebinarItem / DocItem
    ├── utils.py              # sanitize_filename, fix_mojibake, filename_from_url
    ├── moodle_client.py      # MoodleClient: login + shared requests.Session
    ├── course_parser.py      # CourseParser: nested-section tree + HTML -> models
    ├── http_download.py      # segmented, resumable, stall-proof downloader
    ├── video_downloader.py   # VideoDownloader: rutube (yt-dlp) + direct mp4
    ├── file_downloader.py    # FileDownloader: resource files + url modules
    ├── doc_downloader.py     # DocDownloader: page/quiz/feedback -> PDF
    ├── external.py           # download_external: Google Drive / direct files
    ├── webinar.py            # WebinarDownloader: MTS Link timeline -> PiP mp4
    └── orchestrator.py       # CourseGrabber: ties everything together
```

## Design decisions

- **Separation of concerns.** The network layer (`MoodleClient`), parsing
  (`CourseParser`), and the video/file downloaders are separate classes. The
  orchestrator knows nothing about HTML or yt-dlp internals — it only calls
  interfaces.
- **Credentials are never hard-coded.** Sources in increasing priority:
  defaults → `config.json` → environment variables → CLI arguments.
  `config.json` is in `.gitignore`.
- **On-disk layout = course structure (nested).** Everything lands under a
  top-level folder named after the course, then a folder per section, **mirroring
  the nesting** of subsections: `result/<Course>/<NN Section>/<NN Subsection>/…`.
  Top-level sections keep their zero-padded Moodle **section number** as the
  `NN` prefix (stable across runs); nested sections use their 1-based **sibling
  order**. Inside a folder: videos `NN - <title>.mp4`, presentations with their
  original names, linked files (`NN - <name>.ipynb`), and captured documents
  (`NN - <name>.pdf`). The item numbering follows the order on the section page.
- **Recursive section discovery** (`CourseParser.section_tree`). Course 297 uses
  Moodle's **flexible-sections** format, where sections nest arbitrarily deep and
  the collapsed main page hides inner content. The tree is found by a
  breadth-first walk that visits each section page and follows its child
  subsections, assigning each section's **parent on first discovery** and
  deduping — so flexsections' habit of rendering a *leaf* page in its *parent's*
  context (re-listing siblings) is harmless. Activities are attributed to their
  **nearest enclosing section**, and modules are deduped globally by their
  `module-NNN` id. (A flat course like id=86 is just a tree of depth 1, so the
  same code path handles both.)
- **Videos inside `mod/page`.** In id=297 the lesson video is not inline on the
  section page — each lesson is a `modtype_page` activity whose view page embeds
  the rutube player(s). `CourseParser._parse_page` opens every page and extracts
  its rutube/direct videos and any embedded file links; if the page also carries
  real text (or has no video) it is additionally kept as a document.
- **Documents as PDF ([doc_downloader.py](grabber/doc_downloader.py)).** Text
  pages and the view pages of quizzes / feedback / assignments aren't files but
  still hold material, so `DocDownloader` renders the visible content to **PDF**
  (via `xhtml2pdf`, falling back to a self-contained HTML file if unavailable),
  fetching inline images over the authenticated session. Only the already-visible
  view page is captured — **no quiz attempt or submission is ever started**, so
  grades and attempt counts are untouched. A Cyrillic TrueType font is registered
  with reportlab + xhtml2pdf so Russian text renders (the default Helvetica has
  no Cyrillic; `@font-face` is avoided because it leaves a locked temp file on
  Windows).
- **Highest video quality** — yt-dlp selector `best` for rutube (rutube serves
  single muxed streams, so best = highest resolution, up to 1080p for the
  Moodle-hosted videos and 720p for rutube ones).
- **Two video delivery mechanisms** (see [SITE_STRUCTURE.md](SITE_STRUCTURE.md)):
  rutube embeds (inline in a label, or inside a `mod/page`, via yt-dlp) and mp4
  files served straight from Moodle (`pluginfile.php`, via the authenticated
  session). `VideoDownloader` routes by `VideoItem.kind` (`rutube` / `direct`).
- **rutube robustness.** rutube throttles bursts of requests, which yt-dlp
  surfaces as an intermittent "No video formats found!" — the same video lists
  formats fine moments later. `VideoDownloader` (1) checks for an existing file
  **before** any rutube call, so a re-run makes zero API requests, and (2) retries
  the extraction+download a few times with growing backoff on transient errors
  (throttling / 429 / timeouts), while letting a genuinely gone/private video
  fail fast.
- **Linked files ([external.py](grabber/external.py)).** A Moodle `url` module
  usually points at a Google Drive file (a code notebook). The primary strategy
  is to download the real file (Google Drive via `gdown`, or a direct file by
  streaming) so it sits next to the videos; only if that fails do we fall back
  to saving an OS-native link shortcut (`.url` on Windows, `.webloc` on macOS,
  `.desktop` on Linux — see `write_link_shortcut` in `utils.py`).
- **Webinars ([webinar.py](grabber/webinar.py)).** A Moodle `mtslink` module
  embeds an MTS Link (webinar.ru) recording, which is an *event-log timeline* of
  several HLS streams, not one file (see [SITE_STRUCTURE.md](SITE_STRUCTURE.md)).
  `WebinarDownloader` resolves the timeline from the public
  `gw.mts-link.ru/api/.../record` endpoint, downloads each stream with ffmpeg,
  and composites **one mp4** that mirrors the player: the 1080p screen-share as
  the main picture with the speaker's camera cropped to a near-square
  **picture-in-picture** in the top-right corner, and the camera full-frame
  whenever nothing is shared. `cuts` (trimmed intro/outro/pauses) and per-stream
  source offsets are honoured. The output canvas is configurable
  (`webinar_width`/`webinar_height`, default 1920×1080).
- **Segment-based compositor (memory-bounded).** A single giant `filter_complex`
  spanning the whole timeline makes ffmpeg open every stream at once and buffer
  frames while `setpts`-shifted clips wait to line up — for a 3-hour, 15-stream
  webinar that buffering exhausts RAM. Instead the compositor:
  1. builds the continuous **audio** track in one light, audio-only pass
     (`atrim`+`adelay`+`amix` over a full-length silent base);
  2. renders the **video** as short, independent **segments** split at every
     layout change — each opens only the 1-2 streams active then, composited
     over a black canvas of the exact interval length (`overlay … eof_action=
     pass`), with input-level seeking so only that span is decoded;
  3. **concatenates** the segments (`-c copy`) and muxes in the audio.
  At no point are more than a couple of streams (or one segment's frames) in
  memory, regardless of webinar length. Stream downloads and segment encodes both
  run **concurrently** (a thread pool), with the CPU/thread budget *split* across
  the parallel encoders so the total stays within `webinar_cpu_fraction`; NVENC
  parallelism is held at 2 (consumer-driver session limit). A 3-hour webinar
  composites in ~15 min end to end.
- **Presentation slides as the screen.** Some webinars never share a screen
  *video* — the presenter shows an uploaded slide deck instead, delivered as
  per-slide images through `presentation.update` events (no HLS stream). The
  timeline reconstructs these as image-backed "screen" pieces (honouring the
  `isActive` show/hide toggles) and fills only the intervals where no real
  screen-share video is active, so the camera-only webinars finally get a screen
  while the screen-sharing ones are unchanged.
- **Webinar robustness.** Several safeguards, since these encodes are long and
  heavy:
  - *Resource budget.* ffmpeg runs at **below-normal OS priority** (a
    `BELOW_NORMAL_PRIORITY_CLASS` creation flag on Windows; a `nice` prefix on
    Linux/macOS — `preexec_fn` is deliberately avoided as it can deadlock in this
    multithreaded process) and is capped to a fraction of the cores
    (`webinar_cpu_fraction`, default 0.75 → `-threads`) so the desktop never
    locks up.
  - *Timeouts everywhere.* Every ffmpeg call (download, audio, each segment,
    concat, mux, verify) has a timeout, so a wedged process (e.g. a seek past
    end-of-stream) is killed and handled instead of hanging forever.
  - *Encoder with real fallback.* `webinar_encoder` `auto` runs a tiny **test
    encode** to confirm NVENC actually works (ffmpeg lists `h264_nvenc` even with
    no usable GPU) and otherwise falls back to **libx264 on CPU**. Output is
    forced to constant frame rate with a regular keyframe interval so seeking is
    exact.
  - *Per-segment self-repair.* Each segment is probed after rendering; if it
    failed, timed out or came out empty/short it is replaced with a **black clip
    of the exact length**, so one bad edge segment can't wreck the webinar.
  - *Integrity + atomic publish.* A clean ffmpeg exit is trusted for downloads
    (a stream shorter than the API metadata is accepted — pieces are clamped to
    the real probed length so ffmpeg never reads past EOF). The final composite
    is written to a `.part.mp4`, verified (both streams present, duration
    matches, tail decodes, a mid-file seek decodes) and only then atomically
    renamed — a bad result is retried once and never left under the final name.
- **Idempotency.** Already-downloaded files are skipped (`skip_existing`) so a
  long run can be restarted without re-downloading.
- **Segmented download ([http_download.py](grabber/http_download.py)).** The SDO
  server sits on a ~150 ms-RTT path where a single TCP stream ramps slowly
  (slow-start) and **sometimes stalls outright**. So each large file is fetched
  as **N parallel byte-range segments** (`concurrency`, default 4); every
  segment has its own stall timeout (30 s read timeout) and a reconnect/resume
  loop. Segments are written to `.segN` files and then concatenated. Jobs run
  **sequentially**, so the active connection count is always N — including the
  last file (previously file-level parallelism collapsed to a single stream on
  the "tail" and that stream would hang). Total throughput (~4–5 MB/s) is capped
  by the server regardless, so more than N connections buys nothing.
- **File names** are sanitized for Windows (`sanitize_filename`): illegal
  `<>:"/\|?*`, C1 control bytes, reserved names, trailing dots/spaces and length
  are handled. A filename from a `Content-Disposition` header arrives
  Latin-1-decoded, so a UTF-8 name is **mojibake** (`ÐÐ±Ð»Ð°ÑÐ½Ð°Ñ`);
  `file_downloader` prefers the RFC 5987 `filename*=UTF-8''…` form and otherwise
  repairs it with `utils.fix_mojibake` (re-encode Latin-1 → decode UTF-8, only
  when that yields Cyrillic). Whitespace is collapsed with an ASCII-only class:
  a Unicode `\s` would eat the `\x85`/`\xa0` second byte of common Cyrillic
  letters (х, Р) mid-character and corrupt the name unrecoverably. Names are also
  capped by **bytes** (~250), not just characters: Linux/ext4 limits a path
  component to 255 *bytes*, and a 150-char Cyrillic name is ~300 bytes (Windows
  and macOS cap by characters, so this only bites on Linux but is safe
  everywhere); truncation lands on a UTF-8 boundary so no character is split.
- **Path length / Windows MAX_PATH.** The course nests deeply (course → module →
  topic → lesson → file); with long Cyrillic names a full path can exceed
  Windows' 260-char `MAX_PATH`, after which many media players and Explorer
  can't open the file even with the OS long-path option enabled (it requires the
  *application* to be long-path-aware). So each component is also capped by a
  configurable **character** limit (`max_name_length`, default 36 → longest path
  ~249) applied with the order prefix folded *into* the name before sanitizing,
  so one cap bounds the whole component. Set `max_name_length` to `0` to disable
  the character cap and keep full names (sensible on Linux/macOS or with
  long-path-aware tools); the byte cap above still applies.
- **Progress logging.** Every item logs a `▶ … START` line before it runs and a
  `✓ … DONE` / `✗ … FAILED` line after, with an `[n/total]` counter, so progress
  is visible during long downloads. A webinar additionally logs when its streams
  finish downloading and its **local compositing** (CPU/GPU-intensive) begins and
  ends.
- **Video titles** come from rutube metadata (via yt-dlp) for rutube videos;
  the Moodle labels carry no meaningful caption next to the iframe. Direct mp4s
  keep their original file-name stem (the label text is just an editor
  placeholder).
- **ffprobe.** `imageio-ffmpeg` ships only `ffmpeg`, not `ffprobe`. That is
  fine: rutube variants are already muxed, we take a single best stream, and
  yt-dlp downloads/remuxes to mp4 without ffprobe. The ffprobe warning is
  silenced in the log (`_YdlLogger`).

## Verification status

Both course shapes run end to end:

- **id=86 (flat, recorded lessons):** 34 videos (8 rutube 720p + 26 Moodle-hosted
  up to 1080p), 2 presentations, 1 linked Drive notebook — ~22.6 GB.
- **id=297 ("Data Science PRO", nested + webinars):** the recursive walk finds
  **92 sections** (vs 5 for a flat read); a full run produced **30 webinars**
  (composited PiP mp4, integrity-checked), **166 videos** (extracted from 83
  `mod/page` activities), **135 presentation/linked files** and **49 documents**
  (28 text pages + 16 quizzes + 4 feedback + 1 assignment, as PDF). Every webinar
  passed the streams-present / duration / tail-decode / mid-seek scrub checks;
  re-running re-downloads nothing.
