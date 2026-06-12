import multiprocessing

from pathlib import Path
from typing import Annotated

import typer

from pipeline import Options, probe_usm, process_usm
from utils.errors import Cancelled, CharlotteError
from utils.fonts import fetch_font
from utils.keys import load_local_keys
from utils.logger import log, route_logs_to_stderr
from utils.reporter import ConsoleReporter, JsonReporter, Reporter


app = typer.Typer(help="USM video file demuxer and converter")


def collect_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".usm":
            log.error("Error: File must have a .usm extension")
            raise typer.Exit(1)
        return [input_path]

    if input_path.is_dir():
        files = list(input_path.glob("*.usm"))
        if not files:
            log.error("No .usm files found in directory")
            raise typer.Exit(1)
        return files

    log.error(f"Error: {input_path} is not a valid file or directory")
    raise typer.Exit(1)


@app.command()
def demux(
    usm_path: Annotated[str, typer.Argument(help="USM file or directory containing USM files.")],
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
) -> None:
    reporter: Reporter
    if json_output:
        route_logs_to_stderr()
        reporter = JsonReporter()
    else:
        reporter = ConsoleReporter()

    usm_files = collect_files(Path(usm_path))

    if probe:
        keys_data = load_local_keys()
        for usm_file in usm_files:
            probe_usm(usm_file, keys_data, reporter)
        return

    log.info(f"Found {len(usm_files)} USM file(s).")
    Path(output).mkdir(exist_ok=True)
    opts = Options(
        output=output,
        no_cleanup=no_cleanup,
        vapoursynth=vapoursynth,
        crf=custom_crf,
        preset=custom_preset,
        x265_params=custom_x265_params,
        fonts=fetch_font(),
    )

    for usm_file in usm_files:
        try:
            process_usm(usm_file, opts, reporter)
        except Cancelled:
            log.info(f"Cancelled during {usm_file.name}.")
            reporter.event("cancelled", file=usm_file.name)
            return
        except CharlotteError as e:
            reporter.event("error", file=usm_file.name, message=str(e))
            raise typer.Exit(1) from None


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app()
