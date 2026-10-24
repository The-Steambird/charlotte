import msvcrt
import sys
import time

from contextlib import contextmanager

import orjson
import typer

from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

from utils.logger import console, log
from utils.reporter.base import Reporter, Task
from utils.strings import translate


STAGE_LABELS = {
    "demux": translate("STAGE_DEMUX"),
    "crack": translate("STAGE_CRACK"),
    "subtitles": translate("STAGE_SUBTITLES"),
    "ffmpeg": translate("STAGE_FFMPEG"),
    "download": translate("STAGE_DOWNLOAD"),
}


class SpeedColumn(ProgressColumn):
    def __init__(self, unit):
        super().__init__()
        self.unit = unit

    def render(self, task):
        speed = task.finished_speed or task.speed
        text = f"{speed:.1f} {self.unit}/s" if speed else f"-- {self.unit}/s"
        return Text(text, style="progress.data.speed")


def key_pressed_within(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            msvcrt.getch()
            return True
        time.sleep(0.1)
    return False


def pause_before_exit(seconds: int = 5) -> None:
    for remaining in range(seconds, 0, -1):
        sys.stderr.write(f"\r{translate('EXIT_COUNTDOWN', seconds=remaining)} ")
        sys.stderr.flush()
        if key_pressed_within(1):
            break
    sys.stderr.write("\n")


def wait_for_key() -> None:
    sys.stderr.write(translate("PRESS_ANY_KEY") + "\n")
    sys.stderr.flush()
    msvcrt.getch()


def make_progress(unit):
    if unit == "B":
        counters = [DownloadColumn(), TransferSpeedColumn()]
    else:
        counters = [MofNCompleteColumn(), SpeedColumn(unit)]
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        *counters,
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )


class ConsoleReporter(Reporter):
    def log(self, level, msg):
        getattr(log, level)(msg)

    @contextmanager
    def task(self, stage: str, total, unit="it"):
        with make_progress(unit) as progress:
            task_id = progress.add_task(STAGE_LABELS.get(stage, stage), total=total)
            yield Task(self, (progress, task_id), total)

    def update_task(self, handle, current, total):
        progress, task_id = handle
        progress.update(task_id, completed=current)

    def ask(self, prompt, *, default=False):
        return typer.confirm(prompt, default=default)

    def event(self, kind, **data):
        # New keys printed as a keys.json group for copy & paste.
        if kind != "capture_key" or data["known"]:
            return
        if data["video_key"] is not None:
            keys = {"videoKey": data["video_key"]}
        else:
            keys = {"audioKey": data["audio_key"], "aesKey": data["aes_key"]}
        group = {**keys, "parentQuestId": data["parent_quest_id"], "videos": data["videos"]}
        text = orjson.dumps(group, option=orjson.OPT_INDENT_2).decode()
        # Soft wrap so that rich doesn't break long lines. Breaks put newlines into copied content.
        console.print(text, soft_wrap=True, highlight=False, markup=False)
