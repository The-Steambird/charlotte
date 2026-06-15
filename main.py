import multiprocessing

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from decoders.hca import AUDIO_CODECS
from pipeline import Options, probe_usm, process_usm
from utils.errors import Cancelled, CharlotteError
from utils.filter import DEFAULT_CRF, DEFAULT_PRESET
from utils.fonts import fetch_font
from utils.keys import load_local_keys
from utils.languages import AUDIO_LANGUAGES, SUBTITLES_LANGUAGES
from utils.logger import log, route_logs_to_stderr
from utils.reporter import ConsoleReporter, JsonReporter, Reporter
from utils.subtitles import sync_subtitles


app = typer.Typer(help="USM video file demuxer and converter")


AUDIO_CODEC_CHOICES = list(AUDIO_CODECS)
AUDIO_CHOICES = [tag for tag, _ in AUDIO_LANGUAGES.values()]
SUBTITLE_CHOICES = list(SUBTITLES_LANGUAGES)


def choice_metavar(choices: list[str]) -> str:
    """Display flag parameter choices for --help in lowercase."""
    return f"[{'|'.join(choice.lower() for choice in choices)}]"


def choice_normalizer(choices: list[str]):
    """Case-insensitive typer callback mapping input."""
    table = {choice.casefold(): choice for choice in choices}

    def normalize(value: str) -> str:
        canonical = table.get(value.casefold())
        if canonical is None:
            allowed = ", ".join(choice.lower() for choice in choices)
            raise typer.BadParameter(f"must be one of: {allowed}")
        return canonical

    return normalize


def collect_files(input_paths: list[Path], reporter: Reporter) -> list[Path]:
    def fail(message: str, name: str) -> NoReturn:
        log.error(message)
        reporter.event("error", file=name, message=message)
        raise typer.Exit(1)

    files: list[Path] = []
    for path in input_paths:
        if path.is_file():
            if path.suffix.lower() != ".usm":
                fail(f"Not a .usm file: {path}", path.name)
            files.append(path)
        elif path.is_dir():
            found = sorted(path.glob("*.usm"))
            if not found:
                fail(f"No .usm files found in directory: {path}", str(path))
            files.extend(found)
        else:
            fail(f"Not a valid file or directory: {path}", str(path))

    files = list(dict.fromkeys(files))
    if not files:
        fail("No .usm input files provided.", "")
    return files


@app.command()
def demux(
    usm_paths: Annotated[
        list[Path] | None,
        typer.Argument(help="USM file(s) or directory(ies) containing USM files."),
    ] = None,
    output: Annotated[str, typer.Option("--output", "-o", help="Output directory.")] = "output",
    no_cleanup: Annotated[
        bool,
        typer.Option(
            "--no-cleanup",
            "-nc",
            help="Do not delete decoded .ivf, .hca, and subtitle files when done.",
        ),
    ] = False,
    vapoursynth: Annotated[
        bool,
        typer.Option(
            "--vapoursynth",
            "-vs",
            help=(
                "Use VapourSynth for video processing. "
                "Looks for matching .py scripts in vs/ directory."
            ),
        ),
    ] = False,
    crf: Annotated[
        float,
        typer.Option(
            "--crf",
            "-crf",
            help="x265 CRF value for VapourSynth output. A non-default value suppresses the "
            "built-in x265 params (see README).",
        ),
    ] = DEFAULT_CRF,
    preset: Annotated[
        str,
        typer.Option(
            "--preset",
            "-preset",
            help="x265 preset for VapourSynth output. A non-default value suppresses the "
            "built-in x265 params (see README).",
        ),
    ] = DEFAULT_PRESET,
    x265_params: Annotated[
        str,
        typer.Option(
            "--x265-params",
            "-x265",
            help="Custom x265 parameters (colon-separated). See README.md for default values used.",
        ),
    ] = "",
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            "-json",
            help="Emit newline-delimited JSON events on stdout for a GUI/automation frontend.",
        ),
    ] = False,
    probe: Annotated[
        bool,
        typer.Option(
            "--probe",
            "-p",
            help="Read-only check what is available for each file (decryption key, local "
            "subtitles, VapourSynth script).",
        ),
    ] = False,
    key: Annotated[
        int | None,
        typer.Option("--key", "-k", help="Manually supply the decryption key for a single file."),
    ] = None,
    default_audio: Annotated[
        str,
        typer.Option(
            "--default-audio",
            "-da",
            help="Audio language to flag as default.",
            metavar=choice_metavar(AUDIO_CHOICES),
            callback=choice_normalizer(AUDIO_CHOICES),
        ),
    ] = "ja",
    default_subtitle: Annotated[
        str,
        typer.Option(
            "--default-sub",
            "-ds",
            help="Subtitle language code to flag as default.",
            metavar=choice_metavar(SUBTITLE_CHOICES),
            callback=choice_normalizer(SUBTITLE_CHOICES),
        ),
    ] = "en",
    audio_codec: Annotated[
        str,
        typer.Option(
            "--audio-codec",
            "-ac",
            help="Audio codec for muxed tracks.",
            metavar=choice_metavar(AUDIO_CODEC_CHOICES),
            callback=choice_normalizer(AUDIO_CODEC_CHOICES),
        ),
    ] = "flac",
    skip_existing: Annotated[
        bool,
        typer.Option("--skip-existing", "-se", help="Skip .mkv files that already exists."),
    ] = False,
    flat: Annotated[
        bool,
        typer.Option(
            "--flat",
            "-f",
            help="Write .mkv directly into the output directory without a parent folder.",
        ),
    ] = False,
) -> None:
    reporter: Reporter
    if json_output:
        route_logs_to_stderr()
        reporter = JsonReporter()
    else:
        reporter = ConsoleReporter()

    usm_files = collect_files(usm_paths or [], reporter)

    if key is not None and len(usm_files) > 1:
        log.error("--key is only valid with a single input file.")
        raise typer.Exit(1)

    if probe:
        keys_data = load_local_keys()
        for usm_file in usm_files:
            probe_usm(usm_file, keys_data, reporter)
        return

    log.info(f"Found {len(usm_files)} USM file(s).")
    Path(output).mkdir(parents=True, exist_ok=True)
    sync_subtitles(reporter)
    opts = Options(
        output=output,
        no_cleanup=no_cleanup,
        vapoursynth=vapoursynth,
        crf=crf,
        preset=preset,
        x265_params=x265_params,
        fonts=fetch_font(),
        manual_key=key,
        default_audio=default_audio,
        default_subtitle=default_subtitle,
        audio_codec=audio_codec,
        skip_existing=skip_existing,
        flat=flat,
    )

    failures = 0
    for usm_file in usm_files:
        try:
            process_usm(usm_file, opts, reporter)
        except Cancelled:
            log.info(f"Cancelled during {usm_file.name}.")
            reporter.event("cancelled", file=usm_file.name)
            return
        except CharlotteError as e:
            log.error(f"Failed to process {usm_file.name}: {e}")
            reporter.event("error", file=usm_file.name, message=str(e))
            failures += 1

    if failures:
        log.warning(f"{failures} of {len(usm_files)} file(s) failed.")
        raise typer.Exit(1)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app()
