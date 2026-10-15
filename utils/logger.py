import logging

from rich.console import Console
from rich.logging import RichHandler


console = Console(stderr=True)

logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
)

log = logging.getLogger("charlotte")

logging.getLogger("vapoursynth").setLevel(logging.ERROR)
logging.getLogger("fontTools").setLevel(logging.WARNING)
# urllib3 logs redirects at INFO, which would print GitHub's signed asset URL on --update.
logging.getLogger("urllib3").setLevel(logging.WARNING)
