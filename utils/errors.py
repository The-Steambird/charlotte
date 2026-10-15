class CharlotteError(Exception):
    """An expected failure rather than a bug. Engine code raises it instead of exiting, and the
    batch loops in pipeline.py report it against the file and carry on."""


class Cancelled(Exception):
    """The frontend cancelled the batch, which ends it cleanly rather than as an error."""


class Skipped(Exception):
    """The frontend asked to drop the current file and carry on with the batch."""
