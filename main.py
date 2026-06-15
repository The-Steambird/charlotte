import multiprocessing

from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from decoders.hca import AUDIO_CODECS
from pipeline import Options, probe_usm, process_usm
from utils.errors import Cancelled, CharlotteError
from utils.fonts import fetch_font
from utils.keys import load_local_keys
from utils.languages import AUDIO_LANGUAGES, SUBTITLES_LANGUAGES
from utils.logger import log, route_logs_to_stderr
from utils.reporter import ConsoleReporter, JsonReporter, Reporter
from utils.subtitles import sync_subtitles


app = typer.Typer(help="USM video file demuxer and converter")


AudioCodec = StrEnum("AudioCodec", {codec: codec for codec in AUDIO_CODECS})
AudioLanguage = StrEnum("AudioLanguage", {lang: lang for lang, _ in AUDIO_LANGUAGES.values()})
SubtitleLanguage = StrEnum("SubtitleLanguage", {lang: lang for lang in SUBTITLES_LANGUAGES})


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
    custom_crf: Annotated[
        float | None,
        typer.Option(
            "--crf",
            "-crf",
            help="x265 CRF value for VapourSynth output. When set, suppresses the built-in x265 "
            "params (default: 13.5 when unset).",
        ),
    ] = None,
    custom_preset: Annotated[
        str | None,
        typer.Option(
            "--preset",
            "-preset",
            help="x265 preset for VapourSynth output. When set, suppresses the built-in x265 "
            "params (default: slower when unset).",
        ),
    ] = None,
    custom_x265_params: Annotated[
        str,
        typer.Option(
            "--x265-params",
            "-x265",
            help="Custom x265 parameters (colon-separated).",
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
            help="Only report what is available for each file (decryption key, local "
            "subtitles, VapourSynth script). Read-only: nothing is processed or fetched.",
        ),
    ] = False,
    key: Annotated[
        int | None,
        typer.Option(
            "--key",
            "-k",
            help="Manually supply the decryption key (videoKey/key2) for a single file."
            "Bypasses the keys.json lookup and is not written back. Only valid with one file.",
        ),
    ] = None,
    default_audio: Annotated[
        AudioLanguage,
        typer.Option("--default-audio", "-da", help="Audio language to flag as default."),
    ] = AudioLanguage.ja,
    default_subtitle: Annotated[
        SubtitleLanguage,
        typer.Option("--default-sub", "-ds", help="Subtitle language code to flag as default."),
    ] = SubtitleLanguage.EN,
    audio_codec: Annotated[
        AudioCodec,
        typer.Option("--audio-codec", "-ac", help="Audio codec for muxed tracks."),
    ] = AudioCodec.flac,
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
        crf=custom_crf,
        preset=custom_preset,
        x265_params=custom_x265_params,
        fonts=fetch_font(),
        manual_key=key,
        default_audio=default_audio.value,
        default_subtitle=default_subtitle.value,
        audio_codec=audio_codec.value,
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
