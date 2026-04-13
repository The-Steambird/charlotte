import importlib
import multiprocessing
import subprocess
import sys

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typer

from tqdm import tqdm


def ffmpeg_params(ffmpeg_path: Path, output: Path) -> list[str]:
    x265_params = [
        "cutree=0",
        "deblock=-2,-2",
        "no-sao=1",
        "tskip=1",
        "cbqpoffs=-3",
        "qcomp=0.7",
        "lookahead-slices=0",
        "keyint=300",
        "min-keyint=30",
        "max-merge=5",
        "ref=6",
        "bframes=12",
        "rd=4",
        "psy-rd=2.0",
        "psy-rdoq=1.5",
        "aq-mode=3",
        "aq-strength=0.7",
    ]

    x265_params = ":".join(x265_params)

    params = [
        str(ffmpeg_path),
        "-y",
        "-v", "warning",
        "-nostats",
        "-progress", "pipe:2",
        "-f", "yuv4mpegpipe",
        "-i", "pipe:0",
        "-c:v", "libx265",
        "-pix_fmt", "yuv420p10le",
        "-profile:v", "main10",
        "-preset", "slower",
        "-crf", "12",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
        "-x265-params", x265_params,
        str(output),
    ]  # fmt: skip

    return params


def parse_ffmpeg_stderr(process: subprocess.Popen, ffmpeg_progress: tqdm) -> None:
    """Reads FFmpeg stderr to update the progress bar and log errors."""
    expected_keys = {
        "frame",
        "fps",
        "stream_0_0_q",
        "bitrate",
        "total_size",
        "out_time_us",
        "out_time_ms",
        "out_time",
        "dup_frames",
        "drop_frames",
        "speed",
        "progress",
    }

    for raw in iter(process.stderr.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        parts = line.split("=", 1)

        # If this line is a machine-readable progress entry, process it and skip the rest.
        if len(parts) == 2 and parts[0].strip() in expected_keys:
            key, val = parts[0].strip(), parts[1].strip()
            if key == "frame" and val.isdigit():
                ffmpeg_progress.update(int(val) - ffmpeg_progress.n)

            continue

        # If it's not a standard progress key, likely FFmpeg warning/error.
        tqdm.write(line, file=sys.stderr)


def worker(
    file_stem: str,
    output_path: Path,
    queue: multiprocessing.Queue,
) -> None:
    """Multiprocessing worker that performs VapourSynth filtering and FFmpeg piping."""
    typer.echo(f"Applying VapourSynth filter: {file_stem}")

    try:
        importlib.invalidate_caches()
        module = importlib.import_module(f"vs.{file_stem}")
        module = importlib.reload(module)

        filter_chain = getattr(module, "filter_chain", None)
        source = output_path / f"{file_stem}.ivf"
        clip = filter_chain(source)
    except Exception as e:
        typer.echo(f"Error importing VapourSynth script for {file_stem}: {e}", err=True)
        queue.put(False)
        return

    cmd = ffmpeg_params(
        ffmpeg_path=Path.cwd() / "ffmpeg.exe",
        output=output_path / f"{file_stem}_filtered.mkv",
    )

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        typer.echo(
            "ffmpeg not found. Place ffmpeg.exe in the root directory and try again.",
            err=True,
        )
        queue.put(False)
        return

    total_frames = clip.num_frames

    with (
        tqdm(
            total=total_frames,
            desc="VapourSynth",
            unit="frames",
            position=0,
            leave=True,
            dynamic_ncols=True,
        ) as vapoursynth_progress,
        tqdm(
            total=total_frames,
            desc="FFmpeg     ",
            unit="frames",
            position=1,
            leave=True,
            dynamic_ncols=True,
        ) as ffmpeg_progress,
    ):

        def ffmpeg_pipe() -> None:
            try:
                with process.stdin:
                    clip.output(
                        process.stdin,
                        y4m=True,
                        progress_update=lambda current, _: vapoursynth_progress.update(
                            current - vapoursynth_progress.n
                        ),
                    )
            finally:
                if vapoursynth_progress.n < total_frames:
                    vapoursynth_progress.update(total_frames - vapoursynth_progress.n)

        with ThreadPoolExecutor(max_workers=1) as executor:
            vs_future = executor.submit(ffmpeg_pipe)

            # The main thread continues to parse FFmpeg logs while the executor pushes frames
            parse_ffmpeg_stderr(process, ffmpeg_progress)

        return_code = process.wait()

    if vs_future.exception():
        typer.echo(
            f"\nVapourSynth processing failed: {vs_future.exception()}", err=True
        )
        queue.put(False)
        return

    if return_code != 0:
        typer.echo(f"FFmpeg exited with code {return_code}", err=True)
        queue.put(False)
        return

    queue.put(True)


def vapoursynth_filter(
    file_stem: str,
    output_path: Path,
) -> Path | None:
    """
    Spawns VapourSynth filter processing in an isolated memory process. vssource.BestSource seems to
    hold an OS-level file handle to index and read the .ivf file. Because VapourSynth's core
    environment is effectively a global singleton in the Python process and caches these indexers,
    the .ivf file remains locked even after the clip goes out of scope and vs_filter() completes,
    breaking the -nc flag when file.unlink() is called.
    """
    ctx = multiprocessing.get_context()
    queue = ctx.Queue()

    process = ctx.Process(
        target=worker,
        args=(file_stem, output_path, queue),
    )
    process.start()
    process.join()

    if process.exitcode != 0:
        return None

    if not queue.empty():
        return output_path / f"{file_stem}_filtered.mkv"

    return None
