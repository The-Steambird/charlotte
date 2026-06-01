import subprocess
import sys

from pathlib import Path

import typer

from utils import languages
from utils.languages import AUDIO_LANGUAGES
from utils.logger import log


def mux(
    output_path: Path, vs_path: Path | None = None, fonts: tuple[Path, Path] | None = None
) -> None:
    """Mux IVF video and FLAC audio into MKV container using ffmpeg."""
    input_file = output_path / f"{output_path.stem}.ivf"
    if vs_path:
        input_file = vs_path

    flac_files = list(output_path.glob("*.flac"))
    subtitle_files = list(output_path.joinpath("subs").glob("*.ass"))

    if not input_file.exists():
        log.error(f"File not found: {input_file}")
        return

    if not flac_files:
        log.error("No FLAC files found to mux.")
        return

    output_mkv = output_path / f"{output_path.stem}.mkv"

    flac_files.sort(key=lambda x: 0 if "_2.flac" in str(x) else 1)
    subtitle_files.sort(key=lambda x: 0 if "_EN.ass" in str(x) else 1)

    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent.parent
    ffmpeg_path = root / "ffmpeg.exe"

    cmd = [str(ffmpeg_path), "-y", "-v", "error"]

    cmd.extend(["-i", str(input_file)])
    for flac_file in flac_files:
        cmd.extend(["-i", str(flac_file)])
    for subtitle_file in subtitle_files:
        cmd.extend(["-i", str(subtitle_file)])

    cmd.extend(["-map", "0"])
    for i in range(len(flac_files)):
        cmd.extend(["-map", str(i + 1)])
    for i in range(len(subtitle_files)):
        cmd.extend(["-map", str(i + 1 + len(flac_files))])

    cmd.extend(["-c", "copy"])

    for i, flac_file in enumerate(flac_files):
        index = flac_file.stem.split("_")[-1]
        lang = AUDIO_LANGUAGES.get(index, "und")
        cmd.extend([f"-metadata:s:a:{i}", f"language={lang}"])
        cmd.extend([f"-disposition:a:{i}", "default" if lang == "ja" else "0"])

    for i, subtitle_file in enumerate(subtitle_files):
        subtitle_lang = subtitle_file.stem.split("_")[-1]
        lang = languages.get_language(subtitle_lang)
        cmd.extend([f"-metadata:s:s:{i}", f"language={lang}"])
        is_en = "_EN.ass" in str(subtitle_file)
        cmd.extend([f"-disposition:s:{i}", "default+forced" if is_en else "0"])

    if fonts:
        font_ja, font_zh = fonts
        cmd.extend(
            [
                "-attach",
                str(font_ja),
                "-metadata:s:t:0",
                "mimetype=application/x-truetype-font",
                "-attach",
                str(font_zh),
                "-metadata:s:t:1",
                "mimetype=application/x-truetype-font",
            ]
        )

    cmd.append(str(output_mkv))

    log.info(f"Muxing: {output_mkv.name}")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

        stdout, stderr = process.communicate()
        return_code = process.returncode
        if return_code != 0:
            log.error(f"Error muxing video: ffmpeg exited with code {return_code}")
            if stdout:
                log.info(f"stdout: {stdout}")
            if stderr:
                log.error(f"stderr: {stderr}")
            raise typer.Exit(1)

        log.info(f"Created: {output_mkv}")
    except FileNotFoundError:
        log.error("FFmpeg not found.")
        raise typer.Exit(1) from None
