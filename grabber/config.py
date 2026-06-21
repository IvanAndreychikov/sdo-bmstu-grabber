"""Configuration loading for the grabber.

Settings come from (in increasing priority):
  1. built-in defaults
  2. config.json in the project root
  3. environment variables (SDO_USERNAME, SDO_PASSWORD, ...)
  4. command-line arguments
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"


@dataclass
class Config:
    """All knobs the grabber needs to run."""

    username: str = ""
    password: str = ""

    base_url: str = "https://sdo.bmstu.ru"
    course_id: int = 86

    # Sections to walk. ``start_section`` follows the user's request to begin at
    # section=3; ``end_section`` is auto-detected from the course page when None.
    start_section: int = 3
    end_section: int | None = None

    # Where everything is written. Relative paths are resolved against the
    # project root.
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "result")

    # Skip items whose target file already exists (makes re-runs cheap).
    skip_existing: bool = True

    # Parallel byte-range connections used per file. The BMSTU server sits on a
    # ~150 ms-RTT path where a single TCP stream ramps slowly and sometimes
    # stalls; splitting each file into ~4 range segments overlaps the slow-start
    # and lets a stalled segment reconnect without holding up the whole file.
    # The server caps total throughput (~4-5 MB/s) so more than ~4 gives little.
    concurrency: int = 4

    # yt-dlp format selector. ``best`` grabs the single highest-quality muxed
    # stream rutube offers (all rutube variants carry both audio and video).
    video_format: str = "best"

    def resolved_output_dir(self) -> Path:
        path = Path(self.output_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    # -- loading ---------------------------------------------------------------
    @classmethod
    def load(cls, config_path: Path | None = None) -> "Config":
        cfg = cls()
        cfg._apply_file(config_path or DEFAULT_CONFIG_PATH)
        cfg._apply_env()
        return cfg

    def _apply_file(self, path: Path) -> None:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in data.items():
            if hasattr(self, key) and value is not None:
                setattr(self, key, value)

    def _apply_env(self) -> None:
        env_map = {
            "username": "SDO_USERNAME",
            "password": "SDO_PASSWORD",
            "base_url": "SDO_BASE_URL",
            "course_id": "SDO_COURSE_ID",
            "start_section": "SDO_START_SECTION",
            "end_section": "SDO_END_SECTION",
            "output_dir": "SDO_OUTPUT_DIR",
            "concurrency": "SDO_CONCURRENCY",
        }
        for attr, env in env_map.items():
            raw = os.environ.get(env)
            if raw is None:
                continue
            if attr in {"course_id", "start_section", "end_section", "concurrency"}:
                setattr(self, attr, int(raw))
            else:
                setattr(self, attr, raw)

    def validate(self) -> None:
        missing = [n for n in ("username", "password") if not getattr(self, n)]
        if missing:
            raise ValueError(
                "Missing credentials: "
                + ", ".join(missing)
                + ". Set them in config.json, environment variables "
                "(SDO_USERNAME / SDO_PASSWORD) or pass --username / --password."
            )
