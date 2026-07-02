from contextlib import contextmanager

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


STAGE_LABELS = {
    "demux": "Demuxing USM",
    "subtitles": "Subtitles",
    "ffmpeg": "Encoding",
}


class SpeedColumn(ProgressColumn):
    def __init__(self, unit):
        super().__init__()
        self.unit = unit

    def render(self, task):
        speed = task.finished_speed or task.speed
        text = f"{speed:.1f} {self.unit}/s" if speed else f"-- {self.unit}/s"
        return Text(text, style="progress.data.speed")


def progress_columns(unit):
    columns: list[ProgressColumn] = [
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    ]
    if unit == "B":
        columns += [DownloadColumn(), TransferSpeedColumn()]
    else:
        columns += [MofNCompleteColumn(), SpeedColumn(unit)]
    columns.append(TimeRemainingColumn())
    return columns


class ConsoleReporter(Reporter):
    def log(self, level, msg):
        getattr(log, level)(msg)

    @contextmanager
    def task(self, stage: str, total, unit="it"):
        progress = Progress(*progress_columns(unit), console=console, transient=True)
        with progress:
            task_id = progress.add_task(STAGE_LABELS.get(stage, stage), total=total)
            yield Task(self, (progress, task_id), total)

    def update_task(self, handle, current, total):
        progress, task_id = handle
        progress.update(task_id, completed=current)

    def ask(self, prompt, *, default=False):
        return typer.confirm(prompt, default=default)
