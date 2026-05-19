import logging
from rich.logging import RichHandler

FORMAT = "%(message)s"
logging.basicConfig(
    level="INFO",
    format=FORMAT,
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)

log = logging.getLogger("charlotte")

# Suppress VapourSynth INFO/WARNING logs (e.g. dynamic thread reduction, API3 deprecations)
logging.getLogger("vapoursynth").setLevel(logging.ERROR)
