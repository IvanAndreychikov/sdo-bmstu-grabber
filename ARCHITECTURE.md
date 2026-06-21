# sdo-bmstu-grabber — architecture

Purpose: log in to the BMSTU distance-learning system (Moodle), walk the course
sections from a given starting one, and save to disk the **videos (highest
quality)**, **presentations**, and **linked files** (e.g. code notebooks),
preserving the video titles and the course structure as folders.

## Stack and dependencies

- **Python 3.13**, isolated `venv/` in the project root.
- `requests` + `beautifulsoup4` + `lxml` — Moodle login and HTML parsing.
- `yt-dlp` — rutube downloads (understands private embed URLs, picks the best
  quality itself, merges HLS).
- `imageio-ffmpeg` — ships a static `ffmpeg` binary right inside the venv (no
  admin rights), needed by yt-dlp to remux into mp4.
- `gdown` — downloads files that URL modules link to on Google Drive.

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
    ├── models.py             # Section / VideoItem / FileItem (dataclasses)
    ├── utils.py              # sanitize_filename, filename_from_url
    ├── moodle_client.py      # MoodleClient: login + shared requests.Session
    ├── course_parser.py      # CourseParser: Moodle HTML -> models
    ├── http_download.py      # segmented, resumable, stall-proof downloader
    ├── video_downloader.py   # VideoDownloader: rutube (yt-dlp) + direct mp4
    ├── file_downloader.py    # FileDownloader: resource files + url modules
    ├── external.py           # download_external: Google Drive / direct files
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
- **On-disk layout = course structure.** Everything lands under a top-level
  folder named after the course:
  `result/<Course name>/<NN> <Section name>/`. The `NN` prefix is the Moodle
  section number, zero-padded, so on-disk sorting matches the course order.
  Inside a section folder: videos `NN - <title>.mp4`, presentation files with
  their original names, and any linked files (e.g. `NN - <name>.ipynb`). The
  item numbering follows the order on the section page.
- **Highest video quality** — yt-dlp selector `best` for rutube (rutube serves
  single muxed streams, so best = highest resolution, up to 1080p for the
  Moodle-hosted videos and 720p for rutube ones).
- **Two video delivery mechanisms** (see [SITE_STRUCTURE.md](SITE_STRUCTURE.md)):
  rutube embeds (sections 3–4, via yt-dlp) and mp4 files served straight from
  Moodle (`pluginfile.php`, sections 5–15, via the authenticated session).
  `VideoDownloader` routes by `VideoItem.kind` (`rutube` / `direct`).
- **Linked files ([external.py](grabber/external.py)).** A Moodle `url` module
  usually points at a Google Drive file (a code notebook). The primary strategy
  is to download the real file (Google Drive via `gdown`, or a direct file by
  streaming) so it sits next to the videos; only if that fails do we fall back
  to saving an OS-native link shortcut (`.url` on Windows, `.webloc` on macOS,
  `.desktop` on Linux — see `write_link_shortcut` in `utils.py`).
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
  `<>:"/\|?*`, reserved names, trailing dots/spaces and length are handled.
- **Video titles** come from rutube metadata (via yt-dlp) for rutube videos;
  the Moodle labels carry no meaningful caption next to the iframe. Direct mp4s
  keep their original file-name stem (the label text is just an editor
  placeholder).
- **ffprobe.** `imageio-ffmpeg` ships only `ffmpeg`, not `ffprobe`. That is
  fine: rutube variants are already muxed, we take a single best stream, and
  yt-dlp downloads/remuxes to mp4 without ffprobe. The ffprobe warning is
  silenced in the log (`_YdlLogger`).

## Verification status

Full end-to-end run completed successfully:
- login to Moodle — ok;
- course map — 14 content sections (3..16), course title resolved;
- 34 videos downloaded (8 rutube 720p + 26 Moodle-hosted up to 1080p),
  2 presentations (PDF), 1 linked notebook (`11.8 PyTorch.ipynb` from Google
  Drive) — 22.6 GB total under the course folder;
- a segment-concatenated 1.13 GB file validated with `ffmpeg -i`: **1920×1080**,
  h264 + aac, 52:37, valid mp4;
- re-run with everything present re-downloads nothing.
