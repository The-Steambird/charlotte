import ctypes
import json
import msvcrt
import os
import sys

from contextlib import contextmanager
from ctypes import wintypes
from itertools import count

import typer

from tqdm import tqdm

from utils.errors import Cancelled
from utils.logger import log


# Bumped when the stdout event schema changes incompatibly; sent in session_start.
PROTOCOL_VERSION = 1

STAGES = {
    "demux": "Demuxing USM",
    "vapoursynth": "VapourSynth",
    "ffmpeg": "FFmpeg     ",
}


def pipe_peek(fd: int) -> int:
    """Check readable bytes from the pipe without blocking, or -1 if fd is not a
    pipe (console/file stdin) or the other end hung up."""
    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return -1
    available = wintypes.DWORD()
    ok = ctypes.windll.kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
    return available.value if ok else -1


class Reporter:
    def log(self, level, msg):
        raise NotImplementedError

    def task(self, stage, total, unit="it"):
        raise NotImplementedError

    def ask(self, prompt, *, default=False):
        raise NotImplementedError

    def poll_cancel(self):
        # Handles GUI cancel request ({"type": "cancel"}) in pipe.
        # JsonReporter can override to True. Returns False by default
        # as console raises KeyboardInterrupt and the VS worker must never touch stdin
        # (race condition to consume the bytes VS in pipe).
        return False

    def checkpoint(self):
        # Pipeline call this after stages (audio decode, mux, etc.) to check for cancel request and
        # calls poll_cancel(). filter.py calls poll_cancel() directly to avoid wait and properly
        # terminate dangling worker.
        if self.poll_cancel():
            raise Cancelled

    def event(self, kind, **data):
        # Machine-readable structured events. only JsonReporter emits these.
        pass


class ConsoleReporter(Reporter):
    """tqdm progress bars + rich logging."""

    def __init__(self):
        self.depth = 0  # open tasks, so stacked bars get distinct positions

    def log(self, level, msg):
        getattr(log, level)(msg)

    @contextmanager
    def task(self, stage, total, unit="it"):
        bar = tqdm(
            total=total, desc=STAGES.get(stage, stage), unit=unit,
            unit_scale=(unit == "B"), position=self.depth, leave=False, dynamic_ncols=True,
        )  # fmt: skip
        self.depth += 1
        try:
            yield ConsoleTask(bar)
        finally:
            self.depth -= 1
            bar.close()

    def ask(self, prompt, *, default=False):
        return typer.confirm(prompt, default=default)


class ConsoleTask:
    def __init__(self, bar):
        self.bar = bar

    def advance(self, n=1):
        self.bar.update(n)

    def set_completed(self, current):
        self.bar.update(current - self.bar.n)


class QueueReporter(Reporter):
    """Lives in the VapourSynth worker process. Forwards every call onto the queue;
    the parent re-emits via its own reporter. The worker must not touch stdout/stderr
    itself - the parent owns them, and in --json mode stdout carries the event stream."""

    def __init__(self, queue):
        self.queue = queue

    def log(self, level, msg):
        self.queue.put(("log", level, msg))

    @contextmanager
    def task(self, stage, total, unit="it"):
        self.queue.put(("task_start", stage, total, unit))
        try:
            yield QueueTask(self.queue, stage)
        finally:
            self.queue.put(("task_end", stage))

    def ask(self, prompt, *, default=False):
        raise RuntimeError("the worker process cannot ask questions")


class QueueTask:
    def __init__(self, queue, stage):
        self.queue = queue
        self.stage = stage

    def advance(self, n=1):
        self.queue.put(("advance", self.stage, n))

    def set_completed(self, current):
        self.queue.put(("set", self.stage, current))


class JsonReporter(Reporter):
    """NDJSON events on stdout; commands (answers) read from stdin on demand. Human
    logs go to stderr instead - see logger.route_logs_to_stderr."""

    def __init__(self, out=None, stdin=None):
        self.out = out if out is not None else sys.stdout
        self.stdin = stdin if stdin is not None else sys.stdin
        self.counter = count()
        self.cancelled = False
        # Pipe mode (the GUI case): read stdin unbuffered via its fd, so a cancel can
        # be polled without blocking and can't sit invisibly in Python's buffered
        # reader after ask() consumed an earlier line. Console/file/test stdin keeps
        # fd None: ask() falls back to readline and poll_cancel never fires.
        self.buf = b""
        try:
            fd = self.stdin.fileno()
        except AttributeError, OSError, ValueError:
            fd = None
        self.fd = fd if fd is not None and pipe_peek(fd) >= 0 else None
        self.emit({"type": "session_start", "protocol": PROTOCOL_VERSION})

    def emit(self, obj):
        line = json.dumps(obj, ensure_ascii=True)
        try:
            self.out.write(line + "\n")
            self.out.flush()
        except OSError, ValueError:
            pass  # consumer closed the pipe

    def log(self, level, msg):
        self.emit({"type": "log", "level": level, "message": msg})

    @contextmanager
    def task(self, stage, total, unit="it"):
        self.emit(
            {"type": "stage", "stage": stage, "status": "start", "total": total, "unit": unit}
        )
        try:
            yield JsonTask(self, stage, total)
        finally:
            self.emit({"type": "stage", "stage": stage, "status": "end"})

    def event(self, kind, **data):
        self.emit({"type": kind, **data})

    def next_line(self):
        if self.fd is None:
            if self.stdin is None:  # no console (frozen no-console build)
                return None
            return self.stdin.readline() or None
        while b"\n" not in self.buf:
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                return None
            if not chunk:
                return None
            self.buf += chunk
        raw, _, self.buf = self.buf.partition(b"\n")
        return raw.decode("utf-8", errors="replace")

    def ask(self, prompt, *, default=False):
        # Blocking read, not a background thread: a background thread blocked on stdin
        # deadlocks the VapourSynth worker's spawn on Windows. ask() only runs before
        # the worker spawns, so this is safe.
        if self.cancelled:
            return default
        qid = f"q{next(self.counter)}"
        self.emit({"type": "question", "id": qid, "prompt": prompt, "default": default})
        while True:
            line = self.next_line()
            if line is None:
                return default
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError, TypeError:
                continue
            if not isinstance(cmd, dict):
                continue
            if cmd.get("type") == "cancel":
                self.cancelled = True
                return default
            if cmd.get("type") == "answer" and cmd.get("id") == qid:
                return bool(cmd.get("value", default))

    def poll_cancel(self):
        """Non-blocking: drain any commands waiting on stdin and report whether the
        frontend asked us to stop. Sticky once true. Pipe mode only; closing stdin
        does NOT cancel, it just means no more commands will ever arrive."""
        if self.cancelled or self.fd is None:
            return self.cancelled
        while True:
            n = pipe_peek(self.fd)
            if n <= 0:
                if n < 0:
                    self.fd = None  # frontend hung up
                break
            try:
                self.buf += os.read(self.fd, n)
            except OSError:
                self.fd = None
                break
        while b"\n" in self.buf:
            raw, _, self.buf = self.buf.partition(b"\n")
            try:
                cmd = json.loads(raw)
            except ValueError, TypeError:
                continue
            if isinstance(cmd, dict) and cmd.get("type") == "cancel":
                self.cancelled = True
        return self.cancelled


class JsonTask:
    def __init__(self, reporter, stage, total):
        self.reporter = reporter
        self.stage = stage
        self.total = total
        self.current = 0
        self.last_pct = -1

    def advance(self, n=1):
        self.current += n
        self.send()

    def set_completed(self, current):
        self.current = current
        self.send()

    def send(self):
        # Throttle to whole-percent steps (plus the final tick): ~100 events per stage.
        pct = int(self.current * 100 / self.total) if self.total else 0
        if pct != self.last_pct or (self.total and self.current >= self.total):
            self.last_pct = pct
            self.reporter.emit(
                {
                    "type": "progress",
                    "stage": self.stage,
                    "current": self.current,
                    "total": self.total,
                }
            )
