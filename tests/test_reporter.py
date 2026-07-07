import io

import orjson
import pytest

from utils.errors import Cancelled
from utils.reporter import PROTOCOL_VERSION, JsonReporter


def make_reporter(stdin_text=""):
    """JsonReporter writing to a captured StringIO; StringIO stdin exercises the
    readline (non-pipe) path."""
    return JsonReporter(out=io.StringIO(), stdin=io.StringIO(stdin_text))


def events_of(reporter):
    """Every NDJSON event the reporter has emitted so far, parsed."""
    return [orjson.loads(line) for line in reporter.out.getvalue().splitlines()]


# --- event shapes (the contract with the GUI frontend) ---


def test_session_start_announces_protocol():
    reporter = make_reporter()
    assert events_of(reporter) == [{"type": "session_start", "protocol": PROTOCOL_VERSION}]


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

    progress = [event for event in events_of(reporter) if event["type"] == "progress"]
    # One event per whole percent instead of 1000 (and not fewer), the final tick always lands.
    assert 100 <= len(progress) <= 102
    assert progress[-1]["current"] == 1000


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


def test_cancel_during_ask_sticks():
    reporter = make_reporter('{"type": "cancel"}\n')
    assert reporter.ask("Overwrite?", default=False) is False
    assert reporter.cancel_requested() is True
    with pytest.raises(Cancelled):
        reporter.checkpoint()
