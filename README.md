# sdo-bmstu-grabber

*Read this in other languages: [Русский](README.ru.md).*

Downloads lecture videos, webinars, presentations, linked files (e.g. code
notebooks) and text/quiz pages from the BMSTU distance-learning system
(`sdo.bmstu.ru`, Moodle), preserving the video titles and the full course
structure as folders.

- Videos are fetched at the **highest available quality** — both rutube embeds
  (inline or inside `mod/page` lessons) and videos served straight from Moodle.
- **Webinars** (MTS Link / webinar.ru recordings) are downloaded and composited
  into one picture-in-picture mp4 — the shared screen (or the presenter's slide
  deck) as the main picture with the speaker's camera in the top-right corner
  (NVIDIA NVENC used when present, CPU fallback otherwise).
- **Deeply nested sections** are handled: the course tree is walked recursively,
  so modules → topics → individual lessons are all found and mirrored as nested
  folders.
- **Text pages, quizzes and feedback** are saved as **PDF** (without ever
  starting a quiz attempt), so no written material is lost.
- Linked files (Google Drive notebooks, etc.) are downloaded next to the videos;
  if a link can't be downloaded, a `.url` shortcut is saved instead.
- Re-running **does not re-download** files that are already present.

> Choose the course with `course_id` (and `start_section`) in `config.json` or
> `--course-id` / `--start-section`. E.g. `course_id=86` is the recorded-lessons
> course; `course_id=297` is the webinar-based "Data Science PRO" course.

For details on how the site works see [SITE_STRUCTURE.md](SITE_STRUCTURE.md),
and on the code architecture see [ARCHITECTURE.md](ARCHITECTURE.md).

## Quick start

The tool is cross-platform (Windows, macOS, Linux). All steps from cloning to
running:

**Windows (cmd):**

```cmd
:: 1. Clone the repository and enter the project folder
git clone https://github.com/IvanAndreychikov/sdo-bmstu-grabber sdo-bmstu-grabber
cd sdo-bmstu-grabber

:: 2. Copy the config template
copy config.example.json config.json

:: 3. Open config.json and put in your SDO login/password
::    (optionally change output_dir, start_section, etc.)
notepad config.json

:: 4. Create a virtual environment
py -m venv venv

:: 5. Install dependencies (you do NOT need to install ffmpeg separately —
::    it ships with the imageio-ffmpeg package right inside the venv)
venv\Scripts\python.exe -m pip install -r requirements.txt

:: 6. Run — downloads the whole course from section=3 to the end into result\
venv\Scripts\python.exe main.py
```

**macOS / Linux (bash):**

```bash
# 1. Clone the repository and enter the project folder
git clone https://github.com/IvanAndreychikov/sdo-bmstu-grabber sdo-bmstu-grabber
cd sdo-bmstu-grabber

# 2. Copy the config template
cp config.example.json config.json

# 3. Open config.json and put in your SDO login/password
#    (optionally change output_dir, start_section, etc.)
nano config.json            # or vim / any editor

# 4. Create a virtual environment
python3 -m venv venv

# 5. Install dependencies (ffmpeg ships with imageio-ffmpeg inside the venv)
venv/bin/python -m pip install -r requirements.txt

# 6. Run — downloads the whole course from section=3 to the end into result/
venv/bin/python main.py
```

> On Linux, make sure the shell uses a UTF-8 locale (e.g. `LANG=C.UTF-8` or
> `…UTF-8`) so the Cyrillic course/section folder names are written correctly.
> macOS and most desktop Linux distributions use UTF-8 by default.

Instead of editing `config.json`, credentials and settings can be supplied via
environment variables (`SDO_USERNAME`, `SDO_PASSWORD`, `SDO_OUTPUT_DIR`, ...) or
command-line arguments (see below). On macOS/Linux use `venv/bin/python` wherever
the examples below show `venv\Scripts\python.exe`.

## Additional run options

```cmd
:: a single section (for a quick check)
venv\Scripts\python.exe main.py --only-section 3

:: a custom destination folder and without skipping already-downloaded files
venv\Scripts\python.exe main.py --output-dir "D:\courses\dl" --no-skip

:: pass credentials directly on the command line, without config.json
venv\Scripts\python.exe main.py --username my-personal-login --password my-personal-password
```

### Useful flags

| Flag | Purpose |
|---|---|
| `--course-id N` | which course to download (default from config) |
| `--start-section N` | which section to start from (default 3) |
| `--end-section N` | which section to stop at (default: the last one) |
| `--only-section N` | process exactly one section |
| `--output-dir PATH` | where to save the files |
| `--concurrency N` | parallel connections per file (default 4) |
| `--max-name-length N` | max chars per folder/file name (default 36, keeps paths under Windows MAX_PATH); `0` = full names |
| `--no-skip` | re-download even files that already exist |
| `-v` | verbose logging |

## Result

```
result/
├── Продвинутый специалист по анализу больших данных (Middle data scientist)/
│   ├── 03 1. Рекуррентные нейронные сети. LSTM слои/
│   │   ├── 01 - <video title>.mp4
│   │   ├── ...
│   │   └── 06 - Рекуррентные нейронные сети. LSTM.pdf
│   └── 06 3. Обзор библиотеки PyTorch.../
│       ├── 01 - <video title>.mp4
│       └── 06 - 11.8 PyTorch.ipynb        # linked file from Google Drive
└── Наука о данных профессиональный уровень (Data Science PRO)/   # course_id=297
    ├── 07 Вебинары 16748/
    │   ├── 01 - Вебинар 1 от 27.01.2026.mp4     # composited PiP webinar
    │   └── ...
    └── 12 Материалы курса/                       # deeply nested subsections
        └── 01 Модуль 1/
            └── 01 Тема 1. Введение в Big data…/
                └── 01 1.1. Введение в предмет/
                    ├── 01 - Data Science, 1 часть.mp4    # video from a mod/page
                    ├── 02 - 1.1 Введение в Big Data.pdf  # presentation
                    └── 01 - Тест 1.pdf                   # quiz captured as PDF
```

By default everything is saved into the `result/` folder in the project root
(it is in `.gitignore`), under a single top-level folder named after the course.
Videos (`.mp4`), webinars (`.mp4`), presentations (`.pdf`/`.pptx`) and linked
files (`.ipynb`/etc.) sit side by side in the section folder — they are separate
files.
