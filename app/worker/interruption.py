"""Stopping a worker without failing the job it was running.

A spot instance gets two minutes' warning before AWS takes it back, and
scale-in stops workers the same way: `docker stop` sends SIGTERM, then kills.
Without this module the job that worker was running stayed `processing`
until the reaper's lease ran out and was then marked failed -- an error for
the uploader about a video that was never at fault.

The pieces:

* `run` stands in for `subprocess.run` everywhere the worker starts FFmpeg
  or ffprobe. It remembers each running process so a stop can end it at
  once, and it refuses to start a new one after a stop -- otherwise a job
  between two encodes would start the second and be killed mid-way anyway.
* `stop_on_sigterm` wraps one job: SIGTERM sets the stop flag and ends the
  running processes, so the step fails in seconds instead of minutes.
* The caller (app.worker.tasks) sees `stop_requested()` and gives the job
  back instead of recording a failure.
"""

import contextlib
import signal
import subprocess
import threading

_lock = threading.Lock()
_running: set[subprocess.Popen] = set()
_stop = threading.Event()


class Interrupted(RuntimeError):
    """A process was not started because the worker is stopping."""


def stop_requested() -> bool:
    return _stop.is_set()


def request_stop() -> None:
    """Stop: refuse new processes and end the running ones."""
    _stop.set()
    with _lock:
        running = list(_running)
    for process in running:
        # terminate, not kill: FFmpeg exits on SIGTERM within milliseconds and
        # the step sees a non-zero exit, which is all it needs to unwind.
        with contextlib.suppress(OSError):
            process.terminate()


def reset() -> None:
    _stop.clear()


def run(command, *, capture_output=False, text=False, timeout=None, check=False):
    """`subprocess.run`, with the process registered while it runs."""
    if _stop.is_set():
        raise Interrupted(f"not starting {command[0]}: the worker is stopping")
    pipe = subprocess.PIPE if capture_output else None
    with subprocess.Popen(command, stdout=pipe, stderr=pipe, text=text) as process:
        with _lock:
            _running.add(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            with _lock:
                _running.discard(process)
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


@contextlib.contextmanager
def stop_on_sigterm():
    """Turn SIGTERM into a stop request for as long as one job runs.

    Chains to whatever handled SIGTERM before, when that was a function: on
    a worker that runs jobs in its own process (RQ's SimpleWorker) that is
    RQ's warm shutdown, which still has to happen. In a forked work horse
    the previous handler is the default action -- terminating -- and is
    exactly what must not run.
    """
    previous = signal.getsignal(signal.SIGTERM)

    def on_sigterm(signum, frame):
        request_stop()
        if callable(previous):
            previous(signum, frame)

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
        reset()
