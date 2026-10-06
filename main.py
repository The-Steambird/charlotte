import multiprocessing
import sys

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from pipeline import Options, crack_all, probe_all, process_all
from resources.fonts import fetch_font
from resources.keys import Keys
from resources.subtitles import sync_subtitles
from stages.filter import DEFAULT_CRF, DEFAULT_PRESET
from utils.ffmpeg import AUDIO_CODECS
from utils.languages import AUDIO_LANGUAGES, SUBTITLES_LANGUAGES
from utils.logger import log
from utils.reporter import ConsoleReporter, JsonReporter, Reporter
from utils.reporter.json import force_utf8
from utils.strings import translate
from utils.update import clear_stale_binary, run_update
from utils.version import __version__


if TYPE_CHECKING:
    from collections.abc import Callable

    from typer.models import OptionInfo


app = typer.Typer(help=translate("APP_HELP"))


AUDIO_CODEC_CHOICES = list(AUDIO_CODECS)
AUDIO_CHOICES = [tag for tag, _ in AUDIO_LANGUAGES.values()]
SUBTITLE_CHOICES = list(SUBTITLES_LANGUAGES)


def choice_normalizer(choices: list[str]) -> Callable[[str], str]:
    canonical_by_key = {choice.casefold(): choice for choice in choices}
    allowed = ", ".join(choice.lower() for choice in choices)

    def normalize(value: str) -> str:
        canonical = canonical_by_key.get(value.casefold())
        if canonical is None:
            raise typer.BadParameter(translate("CHOICE_INVALID", choices=allowed))
        return canonical

    return normalize


def choice_option(*names: str, help: str, choices: list[str]) -> OptionInfo:
    return typer.Option(
        *names,
        help=help,
        metavar=f"[{'|'.join(choice.lower() for choice in choices)}]",
        callback=choice_normalizer(choices),
    )


def die(message: str) -> NoReturn:
    log.error(message)
    raise typer.Exit(1)


def collect_files(input_paths: list[Path], reporter: Reporter) -> list[Path]:
    def fail(message: str, name: str) -> NoReturn:
        reporter.event("error", file=name, message=message)
        die(message)

    if not input_paths:
        fail(translate("NO_INPUT_FILES"), "")

    files: list[Path] = []
    for path in input_paths:
        if path.is_file():
            if path.suffix.lower() != ".usm":
                fail(translate("NOT_USM_FILE", path=path), path.name)
            files.append(path)
        elif path.is_dir():
            found = sorted(path.glob("*.usm"))
            if not found:
                fail(translate("NO_USM_IN_DIRECTORY", path=path), str(path))
            files.extend(found)
        else:
            fail(translate("NOT_FILE_OR_DIRECTORY", path=path), str(path))

    return list(dict.fromkeys(files))


@app.command()
def demux(
    usm_paths: Annotated[
        list[Path] | None,
        typer.Argument(help=translate("HELP_INPUT")),
    ] = None,
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help=translate("HELP_OUTPUT")),
    ] = Path("output"),
    no_cleanup: Annotated[
        bool,
        typer.Option("--no-cleanup", "-nc", help=translate("HELP_NO_CLEANUP")),
    ] = False,
    vapoursynth: Annotated[
        bool,
        typer.Option("--vapoursynth", "-vs", help=translate("HELP_VAPOURSYNTH")),
    ] = False,
    hard_sub: Annotated[
        bool,
        typer.Option("--hard-sub", "-hs", help=translate("HELP_HARD_SUB")),
    ] = False,
    crf: Annotated[
        float,
        typer.Option("--crf", "-crf", help=translate("HELP_CRF")),
    ] = DEFAULT_CRF,
    preset: Annotated[
        str,
        typer.Option("--preset", "-preset", help=translate("HELP_PRESET")),
    ] = DEFAULT_PRESET,
    x265_params: Annotated[
        str | None,
        typer.Option("--x265-params", "-x265", help=translate("HELP_X265_PARAMS")),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-json", help=translate("HELP_JSON")),
    ] = False,
    probe: Annotated[
        bool,
        typer.Option("--probe", "-p", help=translate("HELP_PROBE")),
    ] = False,
    crack: Annotated[
        bool,
        typer.Option("--crack", "-c", help=translate("HELP_CRACK")),
    ] = False,
    update: Annotated[
        bool,
        typer.Option("--update", "-u", help=translate("HELP_UPDATE")),
    ] = False,
    key: Annotated[
        str | None,
        typer.Option("--key", "-k", help=translate("HELP_KEY")),
    ] = None,
    default_audio: Annotated[
        str,
        choice_option(
            "--default-audio",
            "-da",
            help=translate("HELP_DEFAULT_AUDIO"),
            choices=AUDIO_CHOICES,
        ),
    ] = "ja",
    default_subtitle: Annotated[
        str,
        choice_option(
            "--default-sub",
            "-ds",
            help=translate("HELP_DEFAULT_SUB"),
            choices=SUBTITLE_CHOICES,
        ),
    ] = "en",
    audio_codec: Annotated[
        str,
        choice_option(
            "--audio-codec",
            "-ac",
            help=translate("HELP_AUDIO_CODEC"),
            choices=AUDIO_CODEC_CHOICES,
        ),
    ] = "flac",
    skip_existing: Annotated[
        bool,
        typer.Option("--skip-existing", "-se", help=translate("HELP_SKIP_EXISTING")),
    ] = False,
    flat: Annotated[
        bool,
        typer.Option("--flat", "-f", help=translate("HELP_FLAT")),
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", "-v", help=translate("HELP_VERSION")),
    ] = False,
) -> None:
    clear_stale_binary()

    if version:
        typer.echo(f"Charlotte {__version__}.")
        raise typer.Exit(0)

    reporter = JsonReporter() if json_output else ConsoleReporter()

    if update:
        if usm_paths or probe or crack or key is not None:
            die(translate("UPDATE_WITH_OTHER_MODES"))
        run_update(reporter, json_output)
        return
    if crack and (probe or key is not None):
        die(translate("CRACK_WITH_OTHER_MODES"))

    usm_files = collect_files(usm_paths or [], reporter)
    if key is not None and len(usm_files) > 1:
        die(translate("KEY_WITH_MANY_FILES"))

    if crack:
        crack_all(usm_files, reporter)
        return

    if probe:
        probe_all(usm_files, Keys(reporter), reporter)
        return

    log.info(translate("FOUND_USM_FILES", count=len(usm_files)))
    keys = Keys(reporter, manual_key=key)

    output.mkdir(parents=True, exist_ok=True)
    sync_subtitles(reporter)
    opts = Options(
        output=output,
        no_cleanup=no_cleanup,
        vapoursynth=vapoursynth,
        crf=crf,
        preset=preset,
        x265_params=x265_params,
        fonts=fetch_font(),
        default_audio=default_audio,
        default_subtitle=default_subtitle,
        audio_codec=audio_codec,
        skip_existing=skip_existing,
        flat=flat,
        hard_sub=hard_sub,
    )

    failures = process_all(usm_files, opts, keys, reporter)
    if failures:
        log.warning(translate("FILES_FAILED", failed=failures, total=len(usm_files)))
        raise typer.Exit(1)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    force_utf8(sys.stdout)
    force_utf8(sys.stderr)
    app()
