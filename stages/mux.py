from typing import TYPE_CHECKING

from utils.errors import CharlotteError
from utils.ffmpeg import run_ffmpeg
from utils.languages import AUDIO_LANGUAGES, get_language
from utils.logger import log


if TYPE_CHECKING:
    from pathlib import Path


def mux_args(
    output_path: Path,
    output_file: Path,
    codec_args: list[str],
    fonts: list[Path] | None = None,
    default_audio: str = "ja",
    default_subtitle: str = "EN",
    audio_extension: str = ".flac",
    subtitles: bool = True,
) -> list[str]:
    """The caller supplies the video input ahead of these. A hard-subbed video gets no soft
    subtitle tracks because players that auto-select one would draw it over the burnt
    subtitle and Matroska has no flag that stops them. The fonts exist for those tracks,
    which is why they are only attached alongside them."""
    audio_files = list(output_path.glob(f"*{audio_extension}"))
    subtitle_files = list(output_path.joinpath("subs").glob("*.ass")) if subtitles else []

    if not audio_files:
        raise CharlotteError("No audio files found to mux.")

    audio_files.sort(
        key=lambda x: (
            0
            if AUDIO_LANGUAGES.get(x.stem.split("_")[-1], ("und", "Unknown"))[0] == default_audio
            else 1
        )
    )
    subtitle_files.sort(key=lambda x: 0 if x.stem.split("_")[-1] == default_subtitle else 1)

    args = []
    for audio_file in audio_files:
        args.extend(["-i", str(audio_file)])
    for subtitle_file in subtitle_files:
        args.extend(["-i", str(subtitle_file)])

    args.extend(["-map", "0"])
    for i in range(len(audio_files)):
        args.extend(["-map", str(i + 1)])
    for i in range(len(subtitle_files)):
        args.extend(["-map", str(i + 1 + len(audio_files))])

    args.extend(codec_args)

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

    for i, font in enumerate(fonts if subtitle_files and fonts else []):
        args.extend(
            [
                "-attach",
                str(font),
                f"-metadata:s:t:{i}",
                "mimetype=application/x-truetype-font",
                f"-metadata:s:t:{i}",
                f"filename={font.name}",
            ]
        )

    args.append(str(output_file))
    return args


def mux(
    output_path: Path,
    fonts: list[Path] | None = None,
    default_audio: str = "ja",
    default_subtitle: str = "EN",
    audio_extension: str = ".flac",
) -> None:
    """Mux the lossless IVF video and the audio into an MKV container using ffmpeg."""
    input_file = output_path / f"{output_path.stem}.ivf"
    if not input_file.exists():
        raise CharlotteError(f"Mux input not found: {input_file.name}")

    output_mkv = output_path / f"{output_path.stem}.mkv"
    args = [
        "-i",
        str(input_file),
        *mux_args(
            output_path,
            output_mkv,
            ["-c", "copy"],
            fonts=fonts,
            default_audio=default_audio,
            default_subtitle=default_subtitle,
            audio_extension=audio_extension,
        ),
    ]

    log.info(f"Muxing: {output_mkv.name}")
    run_ffmpeg(args, "Muxing failed")
    log.info(f"Created: {output_mkv}")
