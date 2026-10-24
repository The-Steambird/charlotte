import multiprocessing
import os

from pathlib import Path
from typing import Annotated, NoReturn
lazy from collections.abc import Callable

import typer

lazy from typer.models import OptionInfo

from pipeline import Options, crack_all, probe_all, process_all
from resources.fonts import fetch_fonts
from resources.keys import Keys
from resources.subtitles import sync_subtitles
from stages.encode import DEFAULT_CRF, DEFAULT_PRESET
from stages.hca import AUDIO_CODECS
from utils.elevation import is_admin, relaunch_as_admin
from utils.errors import CharlotteError
from utils.languages import AUDIO_LANGUAGES, SUBTITLES_LANGUAGES
from utils.logger import ReporterHandler, log
from utils.reporter import ConsoleReporter, JsonReporter, Reporter
from utils.reporter.console import wait_for_key
from utils.reporter.json import open_pipe
from utils.strings import strings, translate
from utils.update import clear_stale_files, run_update
from utils.version import __version__
lazy from capture.run import KeyCapture


app = typer.Typer(help=translate("APP_HELP"))


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


def die(message: str, reporter: Reporter, name: str = "") -> NoReturn:
    reporter.event("error", file=name, message=message)
    log.error(message, extra={"sent_as_event": True})
    raise typer.Exit(1)


def collect_files(input_paths: list[Path], reporter: Reporter) -> list[Path]:
    if not input_paths:
        die(translate("NO_INPUT_FILES"), reporter)

    files: list[Path] = []
    for path in input_paths:
        if path.is_file():
            if path.suffix.lower() != ".usm":
                die(translate("NOT_USM_FILE", path=path), reporter, path.name)
            files.append(path)
        elif path.is_dir():
            found = sorted(path.glob("*.usm"))
            if not found:
                die(translate("NO_USM_IN_DIRECTORY", path=path), reporter, str(path))
            files.extend(found)
        else:
            die(translate("NOT_FILE_OR_DIRECTORY", path=path), reporter, path.name)

    return list(dict.fromkeys(files))


def run_capture(reporter: Reporter, json_output: bool, pause: bool, legacy: bool) -> None:
    if not is_admin():
        if json_output:
            die(translate("CAPTURE_NEEDS_ADMIN"), reporter)
        if not relaunch_as_admin(["--pause"]):
            die(translate("CAPTURE_ELEVATION_DECLINED"), reporter)
        log.info(translate("CAPTURE_ELEVATED_WINDOW"))
        return

    try:
        KeyCapture(Keys(reporter).data, reporter, legacy).run()
    except CharlotteError as e:
        die(str(e), reporter)
    except Exception as e:
        # The GUI cannot read the stderr of a capture it started and gets the error as an event.
        # The traceback is logged here because typer would print it only after the pause below.
        message = translate("CAPTURE_CRASHED", error=repr(e))
        reporter.event("error", file="", message=message)
        log.exception(message, extra={"sent_as_event": True})
        raise typer.Exit(1) from e
    finally:
        # The elevated window closes with the process, and the keys in it would go too.
        if pause:
            wait_for_key()


def open_reporter(json_output: bool, pipe: str | None, capture: bool) -> Reporter:
    if pipe is None:
        return JsonReporter() if json_output else ConsoleReporter()
    if not (capture and json_output):
        die(translate("PIPE_ONLY_WITH_CAPTURE"), ConsoleReporter())
    try:
        reporter = open_pipe(pipe)
    except (ValueError, OSError) as e:
        die(translate("PIPE_OPEN_FAILED", name=pipe, error=e), ConsoleReporter())
    log.addHandler(ReporterHandler(reporter))
    return reporter


@app.command()
def run(
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
    capture: Annotated[
        bool,
        typer.Option("--capture", "-cap", help=translate("HELP_CAPTURE")),
    ] = False,
    pause: Annotated[bool, typer.Option("--pause", hidden=True)] = False,
    pipe: Annotated[str | None, typer.Option("--pipe", hidden=True)] = None,
    lang: Annotated[str | None, typer.Option("--lang", hidden=True)] = None,
    legacy_capture: Annotated[bool, typer.Option("--legacy-capture", hidden=True)] = False,
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
            choices=list(AUDIO_LANGUAGES.values()),
        ),
    ] = "ja",
    default_subtitle: Annotated[
        str,
        choice_option(
            "--default-sub",
            "-ds",
            help=translate("HELP_DEFAULT_SUB"),
            choices=list(SUBTITLES_LANGUAGES),
        ),
    ] = "en",
    audio_codec: Annotated[
        str,
        choice_option(
            "--audio-codec",
            "-ac",
            help=translate("HELP_AUDIO_CODEC"),
            choices=list(AUDIO_CODECS),
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
    clear_stale_files()
    # The GUI passes its language as a flag, because UAC drops the environment.
    if lang:
        os.environ["CHARLOTTE_LANG"] = lang
        # Translating the help texts on import already cached the default language.
        strings.cache_clear()

    if version:
        typer.echo(f"Charlotte {__version__}.")
        raise typer.Exit(0)

    reporter = open_reporter(json_output, pipe, capture)

    if update:
        if usm_paths or probe or crack or capture or key is not None:
            die(translate("UPDATE_WITH_OTHER_MODES"), reporter)
        run_update(reporter, json_output)
        return
    if capture:
        if usm_paths or probe or crack or key is not None:
            die(translate("CAPTURE_WITH_OTHER_MODES"), reporter)
        run_capture(reporter, json_output, pause, legacy_capture)
        return
    if crack and (probe or key is not None):
        die(translate("CRACK_WITH_OTHER_MODES"), reporter)
    if probe and key is not None:
        die(translate("PROBE_WITH_KEY"), reporter)

    usm_files = collect_files(usm_paths or [], reporter)
    if key is not None and len(usm_files) > 1:
        die(translate("KEY_WITH_MANY_FILES"), reporter)

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
        fonts=fetch_fonts(),
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
    app()
