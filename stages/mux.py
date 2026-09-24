from typing import TYPE_CHECKING

from utils.errors import CharlotteError
from utils.ffmpeg import run_ffmpeg
from utils.languages import AUDIO_LANGUAGES, get_language
from utils.logger import log


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def track_code(path: Path) -> str:
    """Every intermediate's name ends in its channel number or subtitle language."""
    return path.stem.rpartition("_")[2]


def mux_args(
    output_file: Path,
    codec_args: list[str],
    audio_files: list[Path],
    subtitle_files: list[Path],
    fonts: Sequence[Path] = (),
    default_audio: str = "ja",
    default_subtitle: str = "EN",
) -> list[str]:
    """The caller puts the video input ahead of these. Fonts are only attached with subtitle
    tracks, and `-f` is needed because the `.part` name does not tell ffmpeg the format."""
    if not audio_files:
        raise CharlotteError("No audio files found to mux.")

    audio = [(path, AUDIO_LANGUAGES.get(track_code(path), ("und",))[0]) for path in audio_files]
    subtitles = [(path, track_code(path)) for path in subtitle_files]
    audio.sort(key=lambda track: (track[1] != default_audio, track[0].name))
    subtitles.sort(key=lambda track: (track[1] != default_subtitle, track[0].name))

    args = []
    for path, _ in audio + subtitles:
        args.extend(["-i", str(path)])
    for i in range(len(audio) + len(subtitles) + 1):
        args.extend(["-map", str(i)])

    args.extend(codec_args)

    for i, (_, lang) in enumerate(audio):
        args.extend([f"-metadata:s:a:{i}", f"language={lang}"])
        args.extend([f"-disposition:a:{i}", "default" if lang == default_audio else "0"])

    for i, (_, code) in enumerate(subtitles):
        args.extend([f"-metadata:s:s:{i}", f"language={get_language(code)}"])
        args.extend([f"-disposition:s:{i}", "default" if code == default_subtitle else "0"])

    for i, font in enumerate(fonts if subtitles else ()):
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

    args.extend(["-f", "matroska", str(output_file)])
    return args


def mux(
    video: Path,
    output_file: Path,
    audio_files: list[Path],
    subtitle_files: list[Path],
    fonts: Sequence[Path] = (),
    default_audio: str = "ja",
    default_subtitle: str = "EN",
) -> None:
    """Mux the lossless IVF video with the audio and subtitle tracks."""
    if not video.exists():
        raise CharlotteError(f"Mux input not found: {video.name}")

    args = [
        "-i",
        str(video),
        *mux_args(
            output_file,
            ["-c", "copy"],
            audio_files,
            subtitle_files,
            fonts=fonts,
            default_audio=default_audio,
            default_subtitle=default_subtitle,
        ),
    ]

    log.info(f"Muxing: {video.stem}")
    run_ffmpeg(args, "Muxing failed")
