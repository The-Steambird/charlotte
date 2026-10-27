import logging

from rich.console import Console
from rich.logging import RichHandler

lazy from utils.reporter import Reporter


console = Console(stderr=True)

logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
)

log = logging.getLogger("charlotte")


class ReporterHandler(logging.Handler):
    """Sends log lines to the GUI as events, for a run whose stderr nobody reads. A line logged
    with `sent_as_event` already reached the GUI as an error event and is not sent twice."""

    def __init__(self, reporter: Reporter):
        super().__init__()
        self.reporter = reporter

    def emit(self, record: logging.LogRecord):
        if not getattr(record, "sent_as_event", False):
            self.reporter.log(record.levelname.lower(), record.getMessage())


logging.getLogger("vapoursynth").setLevel(logging.ERROR)
# urllib3 logs redirects at INFO, which would print GitHub's signed asset URL on --update.
logging.getLogger("urllib3").setLevel(logging.WARNING)
