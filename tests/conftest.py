import contextlib
import struct
import subprocess
import types

import pytest

import resources.fonts
import resources.keys
import resources.subtitles
import utils.ffmpeg

from utils.reporter import Reporter, Task


def chunk(sig: bytes, payload: bytes, channel: int = 0, data_type: int = 0) -> bytes:
    """One USM chunk: 32-byte header (payload at the standard 0x18 offset, no
    padding) followed by the payload."""
    data_size = 0x18 + len(payload)
    header = struct.pack(">4sIxBHB2xB16x", sig, data_size, 0x18, 0, channel, data_type)
    return header + payload


def flag_value(cmd, flag):
    """The argument following a flag in an ffmpeg argument list,
    e.g. flag_value(cmd, "-preset") == "slower"."""
    return cmd[cmd.index(flag) + 1]


def forbid_call(*args, **kwargs):
    """Stub for anything a code path must not reach (network fetches, the pipeline)."""
    pytest.fail("Must not be called on this code path")


@pytest.fixture
def ffmpeg(monkeypatch):
    """Capture the ffmpeg command instead of running it. `cmd` and `input` record what
    was passed; `returncode`, `stdout`, `stderr` and `missing` script the outcome."""
    capture = types.SimpleNamespace(
        cmd=None, input=None, returncode=0, stdout=b"", stderr=b"", missing=False
    )

    def fake_run(cmd, **kwargs):
        if capture.missing:
            raise FileNotFoundError(cmd[0])
        capture.cmd = cmd
        capture.input = kwargs.get("input")
        return subprocess.CompletedProcess(
            cmd, capture.returncode, stdout=capture.stdout, stderr=capture.stderr
        )

    monkeypatch.setattr(utils.ffmpeg.subprocess, "run", fake_run)
    return capture


class FakeReporter(Reporter):
    """Records everything and answers ask() with `answer`. `open_tasks` counts the
    task() contexts still unclosed, which the worker-queue relay tests check."""

    def __init__(self, answer: bool = False):
        self.answer = answer
        self.logs = []
        self.events = []
        self.prompts = []
        self.tasks = []
        self.progress = []
        self.open_tasks = 0

    def log(self, level, msg):
        self.logs.append((level, msg))

    @contextlib.contextmanager
    def task(self, stage, total, unit="it"):
        self.tasks.append((stage, total, unit))
        self.open_tasks += 1
        try:
            yield Task(self, stage, total)
        finally:
            self.open_tasks -= 1

    def update_task(self, handle, current, total):
        self.progress.append((handle, current))

    def ask(self, prompt, *, default=False):
        self.prompts.append(prompt)
        return self.answer

    def event(self, kind, **data):
        self.events.append((kind, data))


class CancellingReporter(FakeReporter):
    """Reports a pending cancel so the next checkpoint raises."""

    def cancel_requested(self):
        return True


@pytest.fixture
def reporter():
    return FakeReporter()


@pytest.fixture
def out_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out


@pytest.fixture(autouse=True)
def tmp_app_root(tmp_path, monkeypatch):
    """Redirect every module that persists files next to the executable (keys.json,
    Subtitle/, font/) into a scratch dir."""
    for module in (resources.keys, resources.subtitles, resources.fonts):
        monkeypatch.setattr(module, "app_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def clear_upstream_cache():
    """fetch_upstream_keys is functools.cache'd; keep one test's stub out of the next."""
    resources.keys.fetch_upstream_keys.cache_clear()
