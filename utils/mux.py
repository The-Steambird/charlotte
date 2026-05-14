import subprocess

from pathlib import Path

import typer

from utils import languages
from utils.languages import AUDIO_LANGUAGES
from utils.logger import log


def mux(output_path: Path, vs_path: Path | None = None) -> None:
    """Mux IVF video and FLAC audio into MKV container using mkvmerge."""
    # Collect video and audio files
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

    # Build mkvmerge command
    cmd = ["mkvmerge", "-o", str(output_mkv), str(input_file)]

    # Put JP track with EN sub to the top.
    flac_files.sort(key=lambda x: 0 if "_2.flac" in str(x) else 1)
    subtitle_files.sort(key=lambda x: 0 if "_EN.ass" in str(x) else 1)

    for flac_file in flac_files:
        index = flac_file.stem.split("_")[-1]
        cmd.extend(
            [
                "--language",
                f"0:{AUDIO_LANGUAGES.get(index, 'und')}",
                "--default-track-flag",
                f"0:{1 if AUDIO_LANGUAGES.get(index, 'und') == 'ja' else 0}",
                str(flac_file),
            ]
        )

    # Add subtitles.
    for subtitle_file in subtitle_files:
        subtitle_lang = subtitle_file.stem.split("_")[-1]
        cmd.extend(
            [
                "--language",
                f"0:{languages.get_language(subtitle_lang)}",
                "--default-track-flag",
                f"0:{1 if '_EN' in str(subtitle_file) else 0}",
                "--forced-display-flag",
                f"0:{1 if '_EN' in str(subtitle_file) else 0}",
                str(subtitle_file),
            ]
        )

    # Attach fonts.
    font_ja = Path.cwd() / "font" / "ja-jp.ttf"
    font_zh = Path.cwd() / "font" / "zh-cn.ttf"
    if font_ja.exists() and font_zh.exists():
        cmd.extend(
            [
                "--attach-file",
                f"{font_ja}",
                "--attach-file",
                f"{font_zh}",
            ]
        )
    else:
        log.warning(
            "Custom fonts not found in the '/font' directory. Subtitles will use the default system font. Check the README to install official fonts."
        )

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
            log.error(f"Error muxing video: mkvmerge exited with code {return_code}")
            if stdout:
                log.info(f"stdout: {stdout}")
            if stderr:
                log.error(f"stderr: {stderr}")
            raise typer.Exit(1)

        log.info(f"Created: {output_mkv}")
    except FileNotFoundError:
        log.error("mkvmerge not found. Place mkvmerge in the root directory and try again.")
        raise typer.Exit(1) from None
