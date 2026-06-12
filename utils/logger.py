import logging

from rich.console import Console
from rich.logging import RichHandler


FORMAT = "%(message)s"
logging.basicConfig(
    level="INFO",
    format=FORMAT,
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)

log = logging.getLogger("charlotte")

logging.getLogger("vapoursynth").setLevel(logging.ERROR)


def route_logs_to_stderr() -> None:
    """Route normal log to stderr leaving only JSON logs to stdout."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, RichHandler):
            handler.console = Console(stderr=True)
