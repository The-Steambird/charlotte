import unicodedata

from typing import TYPE_CHECKING

from fontTools.subset import Subsetter
from fontTools.ttLib import TTFont

from utils.ffmpeg import run_ffmpeg
from utils.languages import AUDIO_LANGUAGES, get_language
from utils.logger import log
from utils.strings import translate


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def track_code(path: Path) -> str:
    """Every intermediate's name ends in its channel number or subtitle language."""
    return path.stem.rpartition("_")[2]


def subset_fonts(fonts: Sequence[Path], subtitle_files: list[Path], folder: Path) -> list[Path]:
    text = "".join(path.read_text(encoding="utf-8") for path in subtitle_files)
    text += unicodedata.normalize("NFKD", text)
    subsets = []
    for font in fonts:
        with TTFont(font, recalcTimestamp=False) as ttf:
            subsetter = Subsetter()
            subsetter.populate(text=text)
            subsetter.subset(ttf)
            ttf.save(folder / font.name)
        subsets.append(folder / font.name)
    return subsets


def mux_args(
    output_file: Path,
    codec_args: list[str],
    audio_files: list[Path],
    subtitle_files: list[Path],
    *,
    fonts: Sequence[Path],
    default_audio: str,
    default_subtitle: str,
) -> list[str]:
    """The caller puts the video input ahead of these. Fonts are only attached with subtitle
    tracks, and `-f` is needed because the `.part` name does not tell ffmpeg the format."""
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
    *,
    fonts: Sequence[Path],
    default_audio: str,
    default_subtitle: str,
) -> None:
    """Mux the lossless IVF video with the audio and subtitle tracks."""
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

    log.info(translate("MUXING", stem=video.stem))
    run_ffmpeg(args, translate("MUX_FAILED"))
