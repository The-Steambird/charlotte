from contextlib import ExitStack, contextmanager
from queue import Empty

from utils.errors import Cancelled
from utils.reporter.base import Reporter, Task


class QueueReporter(Reporter):
    """Runs in the VapourSynth worker process, forwards every call onto the queue for
    relay_worker() in the parent to replay. The worker must not touch stdout/stderr -
    the parent owns them, and in --json mode stdout carries the event stream."""

    def __init__(self, queue):
        self.queue = queue

    def log(self, level, msg):
        self.queue.put(("log", level, msg))

    @contextmanager
    def task(self, stage, total, unit="it"):
        self.queue.put(("task_start", stage, total, unit))
        try:
            yield Task(self, stage, total)
        finally:
            self.queue.put(("task_end", stage))

    def update_task(self, stage, current, total):
        self.queue.put(("progress", stage, current))

    def ask(self, prompt, *, default=False):
        raise RuntimeError("the worker process cannot ask questions")


def relay_worker(reporter: Reporter, queue, process):
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
                case ("task_end", _):
                    pass

        # Short timeout polls keep a frontend cancel responsive, "result" ends the relay.
        while process.is_alive():
            if reporter.cancel_requested():
                # terminate() kills only the worker, not the ffmpeg it spawned. The dying
                # worker closes the pipe feeding ffmpeg's stdin, so ffmpeg sees EOF and
                # exits on its own. The GUI's Job Object is the backstop.
                process.terminate()
                process.join()
                raise Cancelled
            try:
                msg = queue.get(timeout=0.2)
            except Empty:
                continue
            if msg[0] == "result":
                return msg[1]
            relay(msg)

        # The worker exited. Its queue feeder flushes before the process dies, so a
        # result put just as it left can still be sitting here after is_alive() turned
        # false and drain the rest without blocking.
        while True:
            try:
                msg = queue.get_nowait()
            except Empty:
                return None
            if msg[0] == "result":
                return msg[1]
            relay(msg)
