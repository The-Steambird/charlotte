import queue

import pytest

from conftest import CancellingReporter, FakeReporter
from utils.errors import Cancelled, Skipped
from utils.reporter.worker import QueueReporter, relay_worker


class FakeProcess:
    """`alive` counts down how many is_alive() polls report a running worker."""

    def __init__(self, alive: int = 10):
        self.alive = alive
        self.terminated = False
        self.joined = False

    def is_alive(self):
        self.alive -= 1
        return self.alive > 0

    def terminate(self):
        self.terminated = True

    def join(self):
        self.joined = True


def loaded_queue(*messages):
    q = queue.Queue()
    for message in messages:
        q.put(message)
    return q


# --- QueueReporter (the producer, inside the worker process) ---


def test_queue_reporter_forwards_calls():
    q = queue.Queue()
    reporter = QueueReporter(q)

    reporter.log("warning", "slow")
    with reporter.task("ffmpeg", 10, unit="frame") as task:
        task.advance(3)

    assert list(q.queue) == [
        ("log", "warning", "slow"),
        ("task_start", "ffmpeg", 10, "frame"),
        ("progress", "ffmpeg", 3),
        ("task_end", "ffmpeg"),
    ]


def test_queue_reporter_ends_the_task_even_on_failure():
    """The relay keeps the progress bar open until task_end arrives."""
    q = queue.Queue()
    reporter = QueueReporter(q)

    with pytest.raises(RuntimeError), reporter.task("ffmpeg", 10):
        raise RuntimeError("the filter blew up")

    assert list(q.queue)[-1] == ("task_end", "ffmpeg")


def test_queue_reporter_cannot_ask():
    """stdin belongs to the parent; a prompt from the worker would deadlock."""
    with pytest.raises(RuntimeError, match="cannot ask questions"):
        QueueReporter(queue.Queue()).ask("Overwrite?")


# --- relay_worker (the consumer, back in the parent) ---


def test_relay_replays_messages_onto_the_reporter(reporter):
    q = loaded_queue(
        ("log", "info", "filtering"),
        ("task_start", "ffmpeg", 100, "frame"),
        ("progress", "ffmpeg", 40),
        ("progress", "ffmpeg", 100),
        ("task_end", "ffmpeg"),
        ("result", True),
    )

    assert relay_worker(reporter, q, FakeProcess()) is True
    assert reporter.logs == [("info", "filtering")]
    assert reporter.tasks == [("ffmpeg", 100, "frame")]
    assert reporter.progress == [("ffmpeg", 40), ("ffmpeg", 100)]


def test_relay_stops_at_the_result_leaving_the_rest(reporter):
    q = loaded_queue(("result", False), ("log", "info", "too late"))

    assert relay_worker(reporter, q, FakeProcess()) is False
    assert reporter.logs == []


def test_relay_drains_a_result_the_dying_worker_left_behind(reporter):
    """is_alive() can go false with the result still in flight: the queue feeder
    flushes as the process exits."""
    q = loaded_queue(("log", "info", "done"), ("result", True))

    assert relay_worker(reporter, q, FakeProcess(alive=1)) is True
    assert reporter.logs == [("info", "done")]


def test_relay_returns_none_when_the_worker_sent_no_result(reporter):
    assert relay_worker(reporter, queue.Queue(), FakeProcess(alive=1)) is None


def test_relay_survives_an_empty_poll(reporter):
    """queue.get times out whenever the worker is busy between messages; a quiet
    moment is not the end of the run."""

    class SlowQueue(queue.Queue):
        polls = 0

        def get(self, block=True, timeout=None):
            self.polls += 1
            if self.polls == 1:
                raise queue.Empty
            return super().get(block=False)

    q = SlowQueue()
    q.put(("result", "late"))

    assert relay_worker(reporter, q, FakeProcess()) == "late"
    assert q.polls == 2  # timed out once, then delivered


def test_relay_cancel_terminates_the_worker():
    reporter = CancellingReporter()
    process = FakeProcess()

    with pytest.raises(Cancelled):
        relay_worker(reporter, loaded_queue(("result", True)), process)

    assert process.terminated
    assert process.joined  # no orphan left behind


def test_relay_skip_terminates_the_worker():
    class SkippingReporter(FakeReporter):
        def skip_requested(self):
            return True

    process = FakeProcess()

    with pytest.raises(Skipped):
        relay_worker(SkippingReporter(), loaded_queue(("result", True)), process)

    assert process.terminated
    assert process.joined


def test_relay_closes_open_tasks_on_cancel():
    """A cancel mid-filter must not strand the progress bar on screen."""

    class CancelAfterStart(FakeReporter):
        def cancel_requested(self):
            return bool(self.tasks)  # quiet until the worker's task is open

    reporter = CancelAfterStart()
    q = loaded_queue(("task_start", "ffmpeg", 100, "frame"), ("result", True))

    with pytest.raises(Cancelled):
        relay_worker(reporter, q, FakeProcess())

    assert reporter.tasks == [("ffmpeg", 100, "frame")]
    assert reporter.open_tasks == 0
