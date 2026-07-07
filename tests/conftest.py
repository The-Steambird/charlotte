import contextlib

import pytest

import resources.fonts
import resources.keys
import resources.subtitles

from utils.reporter import Reporter, Task


class FakeReporter(Reporter):
    """Test double: records logs/events/prompts, answers ask() with a scripted response."""

    def __init__(self, answer: bool = False):
        self.answer = answer
        self.logs = []
        self.events = []
        self.prompts = []

    def log(self, level, msg):
        self.logs.append((level, msg))

    @contextlib.contextmanager
    def task(self, stage, total, unit="it"):
        yield Task(self, stage, total)

    def update_task(self, handle, current, total):
        pass

    def ask(self, prompt, *, default=False):
        self.prompts.append(prompt)
        return self.answer

    def event(self, kind, **data):
        self.events.append((kind, data))


@pytest.fixture
def reporter():
    return FakeReporter()


@pytest.fixture(autouse=True)
def tmp_app_root(tmp_path, monkeypatch):
    """Redirect every module that persists files next to the executable (keys.json,
    Subtitle/, font/) into a scratch dir so tests don't affect the real ones."""
    for module in (resources.keys, resources.subtitles, resources.fonts):
        monkeypatch.setattr(module, "app_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def clear_upstream_cache():
    """fetch_upstream_keys is from functools.cache and keep results from leaking across tests."""
    resources.keys.fetch_upstream_keys.cache_clear()
