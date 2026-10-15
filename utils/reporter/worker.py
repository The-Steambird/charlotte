from contextlib import ExitStack, contextmanager
from queue import Empty

from utils.errors import Cancelled, Skipped
from utils.reporter.base import Reporter, Task


class QueueReporter(Reporter):
    """Runs in the VapourSynth worker process and forwards every call onto the queue for
    relay_worker() in the parent to replay. The worker must not touch stdout or stderr, because
    the parent owns them and in --json mode stdout carries the event stream."""

    def __init__(self, queue):
        self.queue = queue

    def log(self, level, msg):
        self.queue.put(("log", level, msg))

    @contextmanager
    def task(self, stage, total, unit="it"):
        self.queue.put(("task_start", stage, total, unit))
        yield Task(self, stage, total)

    def update_task(self, handle, current, total):
        self.queue.put(("progress", handle, current))


def relay_worker(reporter: Reporter, queue, process, stop):
    """Drain the worker's queue onto `reporter` and return its result payload
    (True/False), or None if the worker died without sending one. This is the
    consumer side of the tuples QueueReporter emits."""
    tasks = {}
    with ExitStack() as stack:

        def relay(msg):
            match msg:
                case ("log", level, text):
                    reporter.log(level, text)
                case ("task_start", stage, total, unit):
                    tasks[stage] = stack.enter_context(reporter.task(stage, total=total, unit=unit))
                case ("progress", stage, current):
                    tasks[stage].set_completed(current)

        # The short poll keeps a cancel or skip from the frontend responsive.
        while process.is_alive():
            try:
                reporter.checkpoint()
            except Cancelled, Skipped:
                # The worker kills its ffmpeg when `stop` is set, which terminate() doesn't do.
                # A worker still building the clip has no ffmpeg yet and may not
                # notice the flag for a long time, and is terminated after the grace period.
                stop.set()
                process.join(5.0)
                if process.is_alive():
                    process.terminate()
                    process.join()
                raise
            try:
                msg = queue.get(timeout=0.2)
            except Empty:
                continue
            if msg[0] == "result":
                return msg[1]
            relay(msg)

        # The worker's queue feeder flushes before the process exits, which means a result put
        # just before the exit can still be waiting here after is_alive() turned false.
        while True:
            try:
                msg = queue.get_nowait()
            except Empty:
                return None
            if msg[0] == "result":
                return msg[1]
            relay(msg)
