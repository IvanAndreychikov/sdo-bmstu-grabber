"""Robust file download over an authenticated requests session.

The BMSTU server sits behind a high-latency (~150 ms RTT) path and a single
TCP stream both ramps slowly and occasionally stalls outright. To stay fast and
unstuck we split a large file into several byte-range segments downloaded in
parallel; every segment has its own stall timeout and reconnect/resume loop, so
a hung connection is dropped and continued rather than blocking forever.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_CHUNK = 1 << 18            # 256 KiB
_CONNECT_TIMEOUT = 15      # seconds to establish a connection
_READ_TIMEOUT = 30         # max seconds between bytes before we treat it as a stall
_MAX_ATTEMPTS = 30         # per-segment reconnect attempts
_MIN_SEGMENT = 8 << 20     # don't bother segmenting below 16 MiB total
_PROGRESS_EVERY = 15       # seconds between progress log lines


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    *,
    connections: int = 4,
) -> Path:
    """Download ``url`` to ``dest``, resuming and retrying as needed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = _probe_size(session, url)

    if not size or connections <= 1 or size < 2 * _MIN_SEGMENT:
        _single_stream(session, url, dest, size)
    else:
        _segmented(session, url, dest, size, connections)
    return dest


# -- size probe ---------------------------------------------------------------
def _probe_size(session: requests.Session, url: str) -> int | None:
    try:
        r = session.get(
            url, stream=True, headers={"Range": "bytes=0-0"},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        r.close()
        cr = r.headers.get("Content-Range")  # "bytes 0-0/12345"
        if cr and "/" in cr:
            tail = cr.rsplit("/", 1)[-1]
            if tail.isdigit():
                return int(tail)
        cl = r.headers.get("Content-Length")
        if cl and cl.isdigit() and r.status_code == 200:
            return int(cl)
    except requests.RequestException as exc:
        log.debug("size probe failed for %s: %s", url, exc)
    return None


# -- single stream (small files / unknown size) -------------------------------
def _single_stream(
    session: requests.Session, url: str, dest: Path, size: int | None
) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        if size and have >= size:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with session.get(
                url, stream=True, headers=headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            ) as resp:
                if have and resp.status_code == 200:
                    have = 0  # server ignored Range; restart
                resp.raise_for_status()
                with open(tmp, "wb" if have == 0 else "ab") as fh:
                    for chunk in resp.iter_content(_CHUNK):
                        fh.write(chunk)
            if not size or tmp.stat().st_size >= size:
                break
        except requests.RequestException as exc:
            _backoff(attempt, dest.name, exc)
    else:
        raise RuntimeError(f"giving up on {dest.name} after {_MAX_ATTEMPTS} attempts")
    tmp.replace(dest)


# -- segmented parallel download ----------------------------------------------
def _segmented(
    session: requests.Session, url: str, dest: Path, size: int, connections: int
) -> None:
    bounds = _segment_bounds(size, connections)
    seg_paths = [dest.with_suffix(dest.suffix + f".seg{i}") for i in range(len(bounds))]

    stop = threading.Event()
    monitor = threading.Thread(
        target=_monitor, args=(dest.name, seg_paths, size, stop), daemon=True
    )
    monitor.start()

    errors: list[BaseException] = []
    threads: list[threading.Thread] = []
    for (start, end), seg in zip(bounds, seg_paths):
        t = threading.Thread(
            target=_segment_worker,
            args=(session, url, start, end, seg, dest.name, errors),
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    stop.set()
    if errors:
        raise errors[0]

    _concat(seg_paths, dest)


def _segment_bounds(size: int, connections: int) -> list[tuple[int, int]]:
    step = size // connections
    bounds = []
    for i in range(connections):
        start = i * step
        end = (size - 1) if i == connections - 1 else (start + step - 1)
        bounds.append((start, end))
    return bounds


def _segment_worker(
    session: requests.Session,
    url: str,
    start: int,
    end: int,
    seg: Path,
    name: str,
    errors: list,
) -> None:
    target = end - start + 1
    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            have = seg.stat().st_size if seg.exists() else 0
            if have >= target:
                return
            headers = {"Range": f"bytes={start + have}-{end}"}
            try:
                with session.get(
                    url, stream=True, headers=headers,
                    timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                ) as resp:
                    resp.raise_for_status()
                    with open(seg, "ab") as fh:
                        for chunk in resp.iter_content(_CHUNK):
                            fh.write(chunk)
                            if fh.tell() + 0 >= target:  # got our slice
                                break
                if seg.stat().st_size >= target:
                    return
            except requests.RequestException as exc:
                _backoff(attempt, f"{name}[{start}-{end}]", exc)
        errors.append(
            RuntimeError(f"segment {start}-{end} of {name} failed after retries")
        )
    except BaseException as exc:  # noqa: BLE001 - surface to the caller
        errors.append(exc)


def _concat(seg_paths: list[Path], dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as out:
        for seg in seg_paths:
            with open(seg, "rb") as fh:
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
    tmp.replace(dest)
    for seg in seg_paths:
        seg.unlink(missing_ok=True)


# -- helpers ------------------------------------------------------------------
def _backoff(attempt: int, what: str, exc: Exception) -> None:
    delay = min(2 ** attempt, 20)
    log.warning("    retry %d for %s after %s (sleep %ds)",
                attempt, what, type(exc).__name__, delay)
    time.sleep(delay)


def _monitor(name: str, seg_paths: list[Path], size: int, stop: threading.Event) -> None:
    last = -1
    while not stop.wait(_PROGRESS_EVERY):
        done = sum(p.stat().st_size for p in seg_paths if p.exists())
        pct = int(done * 100 / size) if size else 0
        if pct != last:
            last = pct
            log.info("    %s: %d%% (%.0f/%.0f MB)",
                     name, pct, done / 1e6, size / 1e6)
