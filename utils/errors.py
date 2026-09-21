class CharlotteError(Exception):
    """charlotte errors that are not a bug. Engine code raises this instead of exiting; main.py
    turns it into a CLI exit code or an error event via --json."""


class Cancelled(Exception):
    """Cancelled event as a clean exit, not an error."""


class Skipped(Exception):
    """The frontend asked to drop the current file and carry on with the batch."""
