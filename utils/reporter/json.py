import ctypes
import msvcrt
import os
import sys

from contextlib import contextmanager
from ctypes import wintypes
from itertools import count

import orjson

from utils.reporter.base import Reporter, Task
from utils.version import __version__


def pipe_peek(fd: int) -> int | None:
    """Readable bytes waiting on the pipe (0 if none), or None if fd is not a pipe
    (console/file stdin) or the other end hung up."""
    handle = msvcrt.get_osfhandle(fd)
    available = wintypes.DWORD()
    ok = ctypes.windll.kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
    return available.value if ok else None


def parse_command(line: str | bytes) -> dict | None:
    try:
        cmd = orjson.loads(line)
    except orjson.JSONDecodeError:
        return None
    return cmd if isinstance(cmd, dict) else None


class JsonReporter(Reporter):
    """Writes NDJSON events to stdout and reads commands (answers, cancel and skip) from stdin
    on demand. Ordinary log lines go to stderr through the shared console in logger.py, which
    keeps stdout pure JSON."""

    def __init__(self, out=None, stdin=None):
        self.out = out if out is not None else sys.stdout
        self.stdin = stdin if stdin is not None else sys.stdin
        self.question_counter = count()
        self.cancelled = False
        self.current_file = None
        self.skip_file = None
        self.last_percent = {}
        # On the GUI's pipe, everything reads stdin through the raw fd into stdin_buf rather than
        # Python's buffered stdin. That lets cancel_requested() peek without blocking, and no
        # bytes can hide in a buffer the peek cannot see. A console, a file or a test is not a
        # pipe, which leaves pipe_fd as None, and then ask() falls back to readline() and
        # cancel_requested() never touches stdin.
        self.stdin_buf = b""
        try:
            fd = self.stdin.fileno()
            self.pipe_fd = fd if pipe_peek(fd) is not None else None
        except AttributeError, OSError, ValueError:
            self.pipe_fd = None
        self.emit({"type": "session_start", "version": __version__})

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

    def update_task(self, handle, current, total):
        # This sends at most one event per whole percent. The final tick always lands on a new
        # 100, which is why the guard never drops it.
        percent = int(current * 100 / total) if total else 0
        if percent != self.last_percent.get(handle, -1):
            self.last_percent[handle] = percent
            self.emit({"type": "progress", "stage": handle, "current": current, "total": total})

    def event(self, kind, **data):
        if kind == "job_start":
            self.current_file = data.get("file")
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
            chunk = os.read(self.pipe_fd, 4096)
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
            self.take_command(cmd)
            if self.cancelled:
                return default
            if cmd.get("type") == "answer" and cmd.get("id") == question_id:
                return bool(cmd.get("value", default))

    def take_command(self, cmd: dict) -> None:
        match cmd.get("type"):
            case "cancel":
                self.cancelled = True
            case "skip":
                self.skip_file = cmd.get("file")

    def cancel_requested(self):
        self.drain_commands()
        return self.cancelled

    def skip_requested(self):
        self.drain_commands()
        if self.skip_file is None:
            return False
        wanted = self.skip_file == self.current_file
        self.skip_file = None
        return wanted

    def drain_commands(self):
        """Read every command already waiting on the stdin pipe without blocking. Does nothing
        in non-pipe mode. GUI closing stdin means no more commands will arrive instead of a
        cancel."""
        if self.cancelled or self.pipe_fd is None:
            return
        while True:
            waiting = pipe_peek(self.pipe_fd)
            if waiting is None:  # Frontend hung up
                self.pipe_fd = None
                break
            if waiting == 0:  # Nothing waiting
                break
            self.stdin_buf += os.read(self.pipe_fd, waiting)
        while (raw_line := self.pop_line()) is not None:
            cmd = parse_command(raw_line)
            if cmd is not None:
                self.take_command(cmd)
