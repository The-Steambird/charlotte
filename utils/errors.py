class CharlotteError(Exception):
    """charlotte errors that are not a bug. Engine code raises this instead of exiting; main.py
    turns it into a CLI exit code or an error event via --json."""


class Canceled(Exception):
    """Canceled event as a clean exit, not an error."""
