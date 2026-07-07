from typing import TYPE_CHECKING

from utils.errors import CharlotteError
from utils.ffmpeg import run_ffmpeg
from utils.languages import AUDIO_LANGUAGES, get_language
from utils.logger import log


if TYPE_CHECKING:
    from pathlib import Path


def mux(
    output_path: Path,
    vs_path: Path | None = None,
    fonts: tuple[Path, Path] | None = None,
    default_audio: str = "ja",
    default_subtitle: str = "EN",
    audio_extension: str = ".flac",
) -> None:
    """Mux IVF video and audio into MKV container using ffmpeg."""
    input_file = output_path / f"{output_path.stem}.ivf"
    if vs_path:
        input_file = vs_path

    audio_files = list(output_path.glob(f"*{audio_extension}"))
    subtitle_files = list(output_path.joinpath("subs").glob("*.ass"))

    if not input_file.exists():
        raise CharlotteError(f"Mux input not found: {input_file.name}")

    if not audio_files:
        raise CharlotteError("No audio files found to mux.")

    output_mkv = output_path / f"{output_path.stem}.mkv"

    audio_files.sort(
        key=lambda x: (
            0
            if AUDIO_LANGUAGES.get(x.stem.split("_")[-1], ("und", "Unknown"))[0] == default_audio
            else 1
        )
    )
    subtitle_files.sort(key=lambda x: 0 if x.stem.split("_")[-1] == default_subtitle else 1)

    args = ["-i", str(input_file)]
    for audio_file in audio_files:
        args.extend(["-i", str(audio_file)])
    for subtitle_file in subtitle_files:
        args.extend(["-i", str(subtitle_file)])

    args.extend(["-map", "0"])
    for i in range(len(audio_files)):
        args.extend(["-map", str(i + 1)])
    for i in range(len(subtitle_files)):
        args.extend(["-map", str(i + 1 + len(audio_files))])

    args.extend(["-c", "copy"])

    for i, audio_file in enumerate(audio_files):
        index = audio_file.stem.split("_")[-1]
        lang = AUDIO_LANGUAGES.get(index, ("und", "Unknown"))[0]
        args.extend([f"-metadata:s:a:{i}", f"language={lang}"])
        args.extend([f"-disposition:a:{i}", "default" if lang == default_audio else "0"])

    for i, subtitle_file in enumerate(subtitle_files):
        subtitle_lang = subtitle_file.stem.split("_")[-1]
        lang = get_language(subtitle_lang)
        args.extend([f"-metadata:s:s:{i}", f"language={lang}"])
        is_default = subtitle_lang == default_subtitle
        args.extend([f"-disposition:s:{i}", "default" if is_default else "0"])

    if fonts:
        font_ja, font_zh = fonts
        args.extend(
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

    args.append(str(output_mkv))

    log.info(f"Muxing: {output_mkv.name}")
    run_ffmpeg(args, "Muxing failed")
    log.info(f"Created: {output_mkv}")
