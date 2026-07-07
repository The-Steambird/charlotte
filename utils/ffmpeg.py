import subprocess

from typing import TYPE_CHECKING

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import bundle_root


if TYPE_CHECKING:
    from pathlib import Path


FFMPEG_MISSING = "FFmpeg not found. Place ffmpeg.exe in the root directory and try again."
AUDIO_CODECS = {
    "flac": (".flac", ["-compression_level", "8"]),
    "opus": (".mka", ["-c:a", "libopus", "-b:a", "256k", "-vbr", "on"]),
}


def ffmpeg_path() -> Path:
    return bundle_root() / "ffmpeg.exe"


def run_ffmpeg(args: list[str], error: str, input: bytes | None = None) -> None:
    cmd = [str(ffmpeg_path()), "-y", "-v", "error", *args]
    try:
        result = subprocess.run(cmd, input=input, capture_output=True, check=False)
    except FileNotFoundError:
        raise CharlotteError(FFMPEG_MISSING) from None

    if result.returncode != 0:
        if result.stdout:
            log.info(result.stdout.decode("utf-8", errors="replace"))
        if result.stderr:
            log.error(result.stderr.decode("utf-8", errors="replace"))
        raise CharlotteError(f"{error}: ffmpeg exited with code {result.returncode}")
