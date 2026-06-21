"""Command-line entry point for sdo-bmstu-grabber.

Examples
--------
    python main.py                       # uses config.json / env vars
    python main.py --start-section 3
    python main.py --username u --password p --output-dir D:/courses/dl
    python main.py --only-section 5      # one section only
"""
from __future__ import annotations

import argparse
import logging
import sys

from grabber.config import Config
from grabber.orchestrator import CourseGrabber


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sdo-bmstu-grabber",
        description="Download course videos (rutube) and presentations from "
                    "sdo.bmstu.ru, preserving the course folder structure.",
    )
    p.add_argument("--username", help="SDO login (overrides config/env)")
    p.add_argument("--password", help="SDO password (overrides config/env)")
    p.add_argument("--course-id", type=int, help="Moodle course id (default 86)")
    p.add_argument("--start-section", type=int,
                   help="First section number to process (default 3)")
    p.add_argument("--end-section", type=int,
                   help="Last section number to process (default: last available)")
    p.add_argument("--only-section", type=int,
                   help="Process exactly one section (shortcut for start==end)")
    p.add_argument("--output-dir", help="Destination directory (default ./result)")
    p.add_argument("--concurrency", type=int,
                   help="Parallel downloads (default 4)")
    p.add_argument("--no-skip", action="store_true",
                   help="Re-download even if the target file already exists")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return p


def apply_args(cfg: Config, args: argparse.Namespace) -> None:
    if args.username:
        cfg.username = args.username
    if args.password:
        cfg.password = args.password
    if args.course_id is not None:
        cfg.course_id = args.course_id
    if args.start_section is not None:
        cfg.start_section = args.start_section
    if args.end_section is not None:
        cfg.end_section = args.end_section
    if args.only_section is not None:
        cfg.start_section = args.only_section
        cfg.end_section = args.only_section
    if args.output_dir:
        cfg.output_dir = args.output_dir
    if args.concurrency is not None:
        cfg.concurrency = args.concurrency
    if args.no_skip:
        cfg.skip_existing = False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load()
    apply_args(cfg, args)

    try:
        CourseGrabber(cfg).run()
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        return 130
    except Exception as exc:
        logging.error("Fatal: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
