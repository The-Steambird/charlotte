import ctypes
import msvcrt
import os
import sys

from contextlib import contextmanager
from ctypes import wintypes
from itertools import count

import orjson

from utils.reporter.base import Reporter, Task


PROTOCOL_VERSION = 1


def pipe_peek(fd: int) -> int | None:
    """Readable bytes waiting on the pipe (0 if none), or None if fd is not a pipe
    (console/file stdin) or the other end hung up."""
    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return None
    available = wintypes.DWORD()
    ok = ctypes.windll.kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
    return available.value if ok else None


def parse_command(line: str | bytes) -> dict | None:
    try:
        cmd = orjson.loads(line)
    except orjson.JSONDecodeError, TypeError:
        return None
    return cmd if isinstance(cmd, dict) else None


class JsonReporter(Reporter):
    """NDJSON events on stdout; commands (answers/cancel) read from stdin on demand.
    Regular log go to stderr via the shared console in logger.py, keeping stdout JSON only."""

    def __init__(self, out=None, stdin=None):
        self.out = out if out is not None else sys.stdout
        self.stdin = stdin if stdin is not None else sys.stdin
        self.question_counter = count()
        self.cancelled = False
        self.last_percent = {}  # per-stage, for throttling progress events
        # stdin is read one of two ways:
        # 1. UI frontend: stdin is a real pipe, and everything reads it through the
        # raw fd (self.pipe_fd) into self.stdin_buf, bypassing Python's buffered stdin.
        # It makes cancellation reliable: cancel_requested() can peek the
        # pipe without blocking, and because ask() reads from the same raw fd, no
        # bytes can hide in Python's stdin buffer where that peek wouldn't see them.
        # 2. Console / file / test: stdin is not a pipe (pipe_peek returns None), so
        # self.pipe_fd stays None, ask() falls back to stdin.readline(), and
        # cancel_requested() never touches stdin.
        self.stdin_buf = b""
        try:
            fd = self.stdin.fileno()
            self.pipe_fd = fd if pipe_peek(fd) is not None else None
        except AttributeError, OSError, ValueError:
            self.pipe_fd = None
        self.emit({"type": "session_start", "protocol": PROTOCOL_VERSION})

    def emit(self, event):
        line = orjson.dumps(event).decode("utf-8")
        try:
            self.out.write(line + "\n")
            self.out.flush()
        except OSError, ValueError:
            pass

    def log(self, level, msg):
        self.emit({"type": "log", "level": level, "message": msg})

    @contextmanager
    def task(self, stage, total, unit="it"):
        self.emit(
            {"type": "stage", "stage": stage, "status": "start", "total": total, "unit": unit}
        )
        try:
            yield Task(self, stage, total)
        finally:
            self.last_percent.pop(stage, None)
            self.emit({"type": "stage", "stage": stage, "status": "end"})

    def update_task(self, stage, current, total):
        # Throttle to whole-percent steps (plus the final tick): ~100 events per stage.
        percent = int(current * 100 / total) if total else 0
        final_tick = total and current >= total
        if percent != self.last_percent.get(stage, -1) or final_tick:
            self.last_percent[stage] = percent
            self.emit({"type": "progress", "stage": stage, "current": current, "total": total})

    def event(self, kind, **data):
        self.emit({"type": kind, **data})

    def pop_line(self) -> bytes | None:
        """Remove and return one complete line from stdin_buf, or None if there isn't one."""
        if b"\n" not in self.stdin_buf:
            return None
        line, _, self.stdin_buf = self.stdin_buf.partition(b"\n")
        return line

    def next_line(self):
        if self.pipe_fd is None:
            if self.stdin is None:  # Guard for .exe build
                return None
            return self.stdin.readline() or None
        while (raw_line := self.pop_line()) is None:
            try:
                chunk = os.read(self.pipe_fd, 4096)
            except OSError:
                return None
            if not chunk:
                return None
            self.stdin_buf += chunk
        return raw_line.decode("utf-8", errors="replace")

    def ask(self, prompt, *, default=False):
        # Blocks the main thread on stdin. Reading stdin from a background thread
        # would deadlock the VapourSynth worker's spawn on Windows. Blocking is
        # safe here because ask() only runs before that spawn.
        if self.cancelled:
            return default
        question_id = f"q{next(self.question_counter)}"
        self.emit({"type": "question", "id": question_id, "prompt": prompt, "default": default})
        while True:
            line = self.next_line()
            if line is None:
                return default
            cmd = parse_command(line.strip())
            if cmd is None:
                continue
            if cmd.get("type") == "cancel":
                self.cancelled = True
                return default
            if cmd.get("type") == "answer" and cmd.get("id") == question_id:
                return bool(cmd.get("value", default))

    def cancel_requested(self):
        """Report if the frontend asked to stop, without blocking by draining any commands
        already waiting on the stdin pipe and look for a cancel among them. Does nothing in
        non-pipe mode. GUI closing stdin means no more commands will arrive instead of a cancel."""
        if self.cancelled or self.pipe_fd is None:
            return self.cancelled
        while True:
            waiting = pipe_peek(self.pipe_fd)
            if waiting is None:  # Frontend hung up
                self.pipe_fd = None
                break
            if waiting == 0:  # Nothing waiting
                break
            try:
                self.stdin_buf += os.read(self.pipe_fd, waiting)
            except OSError:
                self.pipe_fd = None
                break
        while (raw_line := self.pop_line()) is not None:
            cmd = parse_command(raw_line)
            if cmd is not None and cmd.get("type") == "cancel":
                self.cancelled = True
        return self.cancelled
