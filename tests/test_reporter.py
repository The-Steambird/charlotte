import io

import orjson
import pytest

from utils.errors import Cancelled
from utils.reporter import PROTOCOL_VERSION, ConsoleReporter, JsonReporter


def make_reporter(stdin_text=""):
    """A StringIO stdin exercises the readline (non-pipe) path."""
    return JsonReporter(out=io.StringIO(), stdin=io.StringIO(stdin_text))


def events_of(reporter):
    return [orjson.loads(line) for line in reporter.out.getvalue().splitlines()]


def progress_of(reporter):
    return [event for event in events_of(reporter) if event["type"] == "progress"]


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


def test_non_ascii_payload_not_dropped():
    """On a cp1252 stdout, UnicodeEncodeError is a ValueError that emit swallows; the
    stream is forced to UTF-8 so release notes and the like survive."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", newline="")
    reporter = JsonReporter(out=stream, stdin=io.StringIO())
    reporter.event("update", notes="もふもふとりさん → v2")
    stream.flush()
    lines = raw.getvalue().decode("utf-8").splitlines()
    assert orjson.loads(lines[-1]) == {"type": "update", "notes": "もふもふとりさん → v2"}


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
    assert 100 <= len(progress) <= 102  # one per whole percent, not one per advance
    assert progress[-1]["current"] == 1000


@pytest.mark.parametrize("advanced, ticks", [(97, [97, 100]), (100, [100])])
def test_set_completed_lands_final_tick_without_duplicating(advanced, ticks):
    """demux ends by snapping the bar to the file size, covering trailing bytes too
    short to be a chunk: from short of the total that is the closing tick, from the
    total it must not duplicate."""
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


def test_cancel_during_ask_sticks():
    reporter = make_reporter('{"type": "cancel"}\n')
    assert reporter.ask("Overwrite?", default=False) is False
    assert reporter.cancel_requested() is True
    with pytest.raises(Cancelled):
        reporter.checkpoint()


# --- ConsoleReporter ---


def test_console_task_tracks_progress():
    reporter = ConsoleReporter()
    with reporter.task("demux", 10, unit="B") as task:
        task.advance(4)
        task.set_completed(10)
        progress, task_id = task.handle
        assert progress.tasks[task_id].completed == 10
    assert task.current == 10
