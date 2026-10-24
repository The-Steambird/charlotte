import contextlib
import ctypes
import io
import logging
import msvcrt
import os

import orjson
import pytest

from utils.errors import Cancelled, Skipped
from utils.logger import ReporterHandler
from utils.reporter import ConsoleReporter, JsonReporter
from utils.reporter.json import open_pipe
from utils.version import __version__


def make_reporter(stdin_text=""):
    """A StringIO stdin exercises the readline (non-pipe) path."""
    return JsonReporter(out=io.StringIO(), stdin=io.StringIO(stdin_text))


def make_piped_reporter(stdin_text=""):
    """A real anonymous pipe exercises the peek-and-drain path the GUI uses."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, stdin_text.encode())
    os.close(write_fd)
    return JsonReporter(out=io.StringIO(), stdin=os.fdopen(read_fd, "rb"))


def events_of(reporter):
    return [orjson.loads(line) for line in reporter.out.getvalue().splitlines()]


def progress_of(reporter):
    return [event for event in events_of(reporter) if event["type"] == "progress"]


# --- event shapes (the contract with the GUI frontend) ---


def test_session_start_announces_version():
    reporter = make_reporter()
    assert events_of(reporter) == [{"type": "session_start", "version": __version__}]


def test_log_event_shape():
    reporter = make_reporter()
    reporter.log("warning", "watch out")
    assert events_of(reporter)[-1] == {"type": "log", "level": "warning", "message": "watch out"}


def test_custom_event_shape():
    reporter = make_reporter()
    reporter.event("job_start", file="a.usm", stem="a")
    assert events_of(reporter)[-1] == {"type": "job_start", "file": "a.usm", "stem": "a"}


def test_stage_events_wrap_progress():
    reporter = make_reporter()
    with reporter.task("demux", 4, unit="chunk") as task:
        task.advance()
        task.advance(3)

    stage = events_of(reporter)[1:]
    assert stage == [
        {"type": "stage", "stage": "demux", "status": "start", "total": 4, "unit": "chunk"},
        {"type": "progress", "stage": "demux", "current": 1, "total": 4},
        {"type": "progress", "stage": "demux", "current": 4, "total": 4},
        {"type": "stage", "stage": "demux", "status": "end"},
    ]


def test_progress_throttled_to_whole_percents():
    reporter = make_reporter()
    with reporter.task("encode", 1000) as task:
        for _ in range(1000):
            task.advance()

    progress = progress_of(reporter)
    assert 100 <= len(progress) <= 102
    assert progress[-1]["current"] == 1000


@pytest.mark.parametrize("advanced, ticks", [(97, [97, 100]), (100, [100])])
def test_set_completed_lands_final_tick_without_duplicating(advanced, ticks):
    """The key recovery sampler can stop short of the end of the file and demux reaches it
    exactly, and both then snap the bar to the file size. The snap is the closing tick in the
    first case and must not repeat it in the second."""
    reporter = make_reporter()
    with reporter.task("demux", 100) as task:
        task.advance(advanced)
        task.set_completed(100)

    assert [event["current"] for event in progress_of(reporter)] == ticks


# --- ask / cancel over stdin ---


def test_ask_emits_question_and_reads_answer():
    reporter = make_reporter('{"type": "answer", "id": "q0", "value": true}\n')
    assert reporter.ask("Overwrite?", default=False) is True
    assert events_of(reporter)[-1] == {
        "type": "question",
        "id": "q0",
        "prompt": "Overwrite?",
        "default": False,
    }


def test_ask_skips_garbage_and_wrong_ids():
    lines = (
        "not json\n"
        '{"type": "answer", "id": "q9", "value": true}\n'
        '{"type": "answer", "id": "q0", "value": false}\n'
    )
    reporter = make_reporter(lines)
    assert reporter.ask("Overwrite?", default=True) is False


def test_ask_returns_default_on_eof():
    reporter = make_reporter("")
    assert reporter.ask("Overwrite?", default=True) is True


def test_piped_ask_keeps_a_cancel_sent_with_the_answer():
    """The GUI's pipe is read through the raw fd. A cancel that arrives in the same write as the
    answer then stays where the next checkpoint can still find it."""
    reporter = make_piped_reporter(
        '{"type": "answer", "id": "q0", "value": true}\n{"type": "cancel"}\n'
    )
    assert reporter.ask("Overwrite?", default=False) is True
    with pytest.raises(Cancelled):
        reporter.checkpoint()


def test_cancel_during_ask_sticks():
    reporter = make_reporter('{"type": "cancel"}\n')
    assert reporter.ask("Overwrite?", default=False) is False
    assert reporter.cancel_requested() is True
    with pytest.raises(Cancelled):
        reporter.checkpoint()


def test_skip_during_ask_is_kept_for_the_checkpoint():
    reporter = make_reporter(
        '{"type": "skip", "file": "Cs_A.usm"}\n{"type": "answer", "id": "q0", "value": true}\n'
    )
    reporter.event("job_start", file="Cs_A.usm")
    assert reporter.ask("Overwrite?", default=False) is True
    with pytest.raises(Skipped):
        reporter.checkpoint()


def test_skip_raises_once_for_the_running_file():
    reporter = make_piped_reporter('{"type": "skip", "file": "Cs_A.usm"}\n')
    reporter.event("job_start", file="Cs_A.usm")
    with pytest.raises(Skipped):
        reporter.checkpoint()
    reporter.checkpoint()


def test_stale_skip_does_not_hit_the_next_file():
    reporter = make_piped_reporter('{"type": "skip", "file": "Cs_A.usm"}\n')
    reporter.event("job_start", file="Cs_B.usm")
    reporter.checkpoint()
    assert reporter.skip_file is None  # dropped rather than left waiting for Cs_A


def test_cancel_outranks_skip():
    reporter = make_piped_reporter('{"type": "skip", "file": "Cs_A.usm"}\n{"type": "cancel"}\n')
    reporter.event("job_start", file="Cs_A.usm")
    with pytest.raises(Cancelled):
        reporter.checkpoint()


@contextlib.contextmanager
def gui_pipe(name):
    """The GUI's end of a capture's named pipe, as a file descriptor."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
    handle = kernel32.CreateNamedPipeW(rf"\\.\pipe\{name}", 3, 0, 1, 65536, 65536, 0, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    fd = msvcrt.open_osfhandle(handle, 0)
    try:
        yield fd
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def test_a_capture_talks_to_the_gui_over_its_named_pipe():
    name = f"charlotte-test-{os.getpid()}"
    with gui_pipe(name) as gui:
        reporter = open_pipe(name)
        reporter.log("info", "x")
        sent = os.read(gui, 4096).decode().splitlines()
        assert [orjson.loads(line)["type"] for line in sent] == ["session_start", "log"]

        os.write(gui, b'{"type": "cancel"}\n')
        assert reporter.cancel_requested()


def test_the_gui_closing_its_pipe_stops_a_capture():
    """The elevated capture is outside the GUI's Job Object. Once the GUI is gone, the pipe is
    the only thing that can stop it."""
    name = f"charlotte-test-hangup-{os.getpid()}"
    with gui_pipe(name) as gui:
        reporter = open_pipe(name)
        os.close(gui)
        assert reporter.cancel_requested()


@pytest.mark.parametrize("name", ["..\\other", "C:/Windows/x", ""])
def test_a_pipe_name_cannot_be_a_path(name):
    with pytest.raises(ValueError, match="not a pipe name"):
        open_pipe(name)


def test_log_lines_reach_a_reporter_whose_stderr_nobody_reads(reporter):
    logger = logging.getLogger("charlotte.test")
    handler = ReporterHandler(reporter)
    logger.addHandler(handler)
    try:
        logger.warning("keys.json not found")
        logger.error("boom", extra={"sent_as_event": True})
    finally:
        logger.removeHandler(handler)
    assert reporter.logs == [("warning", "keys.json not found")]


@pytest.mark.parametrize(
    "keys, expected",
    [
        (
            {"video_key": 9602627639787478, "audio_key": None, "aes_key": None},
            {"videoKey": 9602627639787478},
        ),
        (
            {"video_key": None, "audio_key": 5281416956962385, "aes_key": "f9e9e1c5"},
            {"audioKey": 5281416956962385, "aesKey": "f9e9e1c5"},
        ),
    ],
    ids=["old scheme", "7.1"],
)
def test_a_captured_key_prints_as_a_group_to_paste_into_keys_json(capsys, keys, expected):
    """The field order has to be the one keys.json uses, because the console output is what a
    user copies."""
    ConsoleReporter().event(
        "capture_key", **keys, parent_quest_id="602", known=False, videos=["Cs_A"]
    )
    printed = orjson.loads(capsys.readouterr().err)
    expected = expected | {"parentQuestId": "602", "videos": ["Cs_A"]}
    assert list(printed.items()) == list(expected.items())
