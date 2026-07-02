import logging

from rich.console import Console
from rich.logging import RichHandler


console = Console(stderr=True)

FORMAT = "%(message)s"
logging.basicConfig(
    level="INFO",
    format=FORMAT,
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
)

log = logging.getLogger("charlotte")

logging.getLogger("vapoursynth").setLevel(logging.ERROR)
