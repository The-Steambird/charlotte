from utils.errors import Cancelled


class Task:
    def __init__(self, reporter, handle, total):
        self.reporter = reporter
        self.handle = handle
        self.total = total
        self.current = 0

    def advance(self, n=1):
        self.current += n
        self.reporter.update_task(self.handle, self.current, self.total)

    def set_completed(self, current):
        self.current = current
        self.reporter.update_task(self.handle, self.current, self.total)


class Reporter:
    def log(self, level, msg):
        raise NotImplementedError

    def task(self, stage, total, unit="it"):
        raise NotImplementedError

    def update_task(self, handle, current, total):
        raise NotImplementedError

    def ask(self, prompt, *, default=False):
        raise NotImplementedError

    def cancel_requested(self):
        return False

    def checkpoint(self):
        if self.cancel_requested():
            raise Cancelled

    def event(self, kind, **data):
        pass
