"""Resumable streaming download over an authenticated requests session."""
from __future__ import annotations

import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_CHUNK = 1 << 18  # 256 KiB


def stream_download(
    session: requests.Session,
    url: str,
    dest: Path,
    *,
    resume: bool = True,
    timeout: int = 120,
) -> Path:
    """Download ``url`` to ``dest`` via ``session``, resuming a partial file.

    Writes to a ``.part`` file first, supports HTTP Range resume when the
    server allows it, then atomically renames to ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    have = tmp.stat().st_size if (resume and tmp.exists()) else 0
    headers = {"Range": f"bytes={have}-"} if have else {}

    with session.get(url, stream=True, headers=headers, timeout=timeout) as resp:
        # 206 = server honoured our Range; 200 = full content (restart).
        if have and resp.status_code == 200:
            have = 0  # server ignored Range; rewrite from scratch
        resp.raise_for_status()

        total = _expected_total(resp, have)
        mode = "ab" if have else "wb"
        with open(tmp, mode) as fh:
            done = have
            for chunk in resp.iter_content(_CHUNK):
                fh.write(chunk)
                done += len(chunk)
                _log_progress(dest.name, done, total)

    tmp.replace(dest)
    return dest


def _expected_total(resp: requests.Response, have: int) -> int | None:
    cr = resp.headers.get("Content-Range")  # e.g. "bytes 100-/12345"
    if cr and "/" in cr:
        tail = cr.rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        return have + int(cl)
    return None


_last_pct: dict[str, int] = {}


def _log_progress(name: str, done: int, total: int | None) -> None:
    if not total:
        return
    pct = int(done * 100 / total)
    if pct != _last_pct.get(name) and pct % 20 == 0:
        _last_pct[name] = pct
        log.info("    %s: %d%% (%.0f/%.0f MB)", name, pct, done / 1e6, total / 1e6)
