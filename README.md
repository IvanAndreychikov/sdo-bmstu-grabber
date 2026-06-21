# sdo-bmstu-grabber

*Read this in other languages: [Русский](README.ru.md).*

Downloads lecture videos (rutube) and course presentations from the BMSTU
distance-learning system (`sdo.bmstu.ru`, Moodle), preserving the video titles
and the course structure as folders.

- Videos are fetched at the **highest available quality**.
- The on-disk layout mirrors the course structure: one folder per section.
- Re-running **does not re-download** files that are already present.

For details on how the site works see [SITE_STRUCTURE.md](SITE_STRUCTURE.md),
and on the code architecture see [ARCHITECTURE.md](ARCHITECTURE.md).

## Quick start

All steps from cloning to running (commands for **cmd** on Windows):

```cmd
:: 1. Clone the repository and enter the project folder
git clone <repository-URL> sdo-bmstu-grabber
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

Instead of editing `config.json`, credentials and settings can be supplied via
environment variables (`SDO_USERNAME`, `SDO_PASSWORD`, `SDO_OUTPUT_DIR`, ...) or
command-line arguments (see below).

## Additional run options

```cmd
:: a single section (for a quick check)
venv\Scripts\python.exe main.py --only-section 3

:: a custom destination folder and without skipping already-downloaded files
venv\Scripts\python.exe main.py --output-dir "D:\courses\dl" --no-skip

:: pass credentials directly on the command line, without config.json
venv\Scripts\python.exe main.py --username ivan.andreychikov --password ***
```

### Useful flags

| Flag | Purpose |
|---|---|
| `--start-section N` | which section to start from (default 3) |
| `--end-section N` | which section to stop at (default: the last one) |
| `--only-section N` | process exactly one section |
| `--output-dir PATH` | where to save the files |
| `--no-skip` | re-download even files that already exist |
| `-v` | verbose logging |

## Result

```
result/
└── 03 1. Рекуррентные нейронные сети. LSTM слои/
    ├── 01 - <video title>.mp4
    ├── 02 - <video title>.mp4
    ├── ...
    └── 06 - Рекуррентные нейронные сети. LSTM.pdf
```

By default everything is saved into the `result/` folder in the project root
(it is in `.gitignore`). Videos (`.mp4`) and presentations (`.pdf`/`.pptx`) sit
side by side in the section folder — they are separate files.
