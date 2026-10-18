"""Reading FFmpeg's own progress while it encodes.

`-progress <file>` makes FFmpeg append `key=value` lines as it goes, among them
`out_time_us`, how far into the output it has written. Against the source's
duration that is a percentage. A thread reads the file every few seconds while
the encode runs on the calling thread; it never touches the encode itself.
"""

import threading
from collections.abc import Callable
from pathlib import Path

POLL_SECONDS = 2.0


def percent_done(progress_text: str, duration_seconds: float) -> float | None:
    """The last `out_time_us` in FFmpeg's progress output, as a percentage."""
    if duration_seconds <= 0:
        return None
    last = None
    for line in progress_text.splitlines():
        key, _, value = line.partition("=")
        if key == "out_time_us" and value.strip().isdigit():
            last = int(value)
    if last is None:
        return None
    return 100.0 * (last / 1_000_000) / duration_seconds


class ProgressWatcher:
    """While open, reports the encode's percentage through `report`."""

    def __init__(
        self,
        progress_file: Path,
        duration_seconds: float,
        report: Callable[[float], None],
        *,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._file = progress_file
        self._duration = duration_seconds
        self._report = report
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def __enter__(self) -> "ProgressWatcher":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=self._poll + 1)

    def read_once(self) -> None:
        try:
            text = self._file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        percent = percent_done(text, self._duration)
        if percent is not None:
            self._report(percent)

    def _watch(self) -> None:
        while not self._stop.wait(self._poll):
            self.read_once()
