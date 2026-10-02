import subprocess

from typing import TYPE_CHECKING

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import bundle_root
from utils.strings import translate


if TYPE_CHECKING:
    from pathlib import Path


AUDIO_CODECS = {
    "flac": (".flac", ["-sample_fmt", "s16", "-compression_level", "8"]),
    "opus": (".mka", ["-c:a", "libopus", "-b:a", "256k", "-vbr", "on", "-f", "matroska"]),
}


def ffmpeg_path() -> Path:
    return bundle_root() / "ffmpeg.exe"


def run_ffmpeg(args: list[str], error: str, input: bytes | None = None) -> None:
    cmd = [str(ffmpeg_path()), "-y", "-v", "error", "-nostdin", *args]
    try:
        result = subprocess.run(cmd, input=input, capture_output=True, check=False)
    except FileNotFoundError:
        raise CharlotteError(translate("FFMPEG_MISSING")) from None

    if result.returncode != 0:
        if result.stdout:
            log.info(result.stdout.decode("utf-8", errors="replace"))
        if result.stderr:
            log.error(result.stderr.decode("utf-8", errors="replace"))
        raise CharlotteError(translate("FFMPEG_FAILED", error=error, code=result.returncode))
