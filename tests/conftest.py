import contextlib
import struct
import subprocess
import types

from itertools import pairwise

import pytest

import resources.fonts
import resources.keys
import resources.subtitles
import utils.ffmpeg

from utils.reporter import Reporter, Task


def chunk(sig: bytes, payload: bytes, channel: int = 0, data_type: int = 0) -> bytes:
    data_size = 0x18 + len(payload)
    header = struct.pack(">4sIxBHB2xB16x", sig, data_size, 0x18, 0, channel, data_type)
    return header + payload


def flag_value(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def input_files(cmd):
    return [value for flag, value in pairwise(cmd) if flag == "-i"]


def forbid_call(*args, **kwargs):
    pytest.fail("Must not be called on this code path")


@pytest.fixture
def ffmpeg(monkeypatch):
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
    for module in (resources.keys, resources.subtitles, resources.fonts):
        monkeypatch.setattr(module, "app_root", lambda: tmp_path)
    return tmp_path
