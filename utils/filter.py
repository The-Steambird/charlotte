import importlib
import importlib.util
import multiprocessing
import subprocess
import sys

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import vapoursynth as vs

from tqdm import tqdm

from utils.logger import log


def ffmpeg_params(ffmpeg_path: Path, output: Path, custom_x265_params: str = "") -> list[str]:
    if custom_x265_params:
        x265_params = custom_x265_params
    else:
        x265_params_list = [
            "cutree=0",
            "deblock=-2,-2",
            "no-sao=1",
            "tskip=1",
            "cbqpoffs=-3",
            "qcomp=0.72",
            "lookahead-slices=0",
            "keyint=300",
            "min-keyint=30",
            "max-merge=5",
            "ref=6",
            "bframes=8",
            "rd=4",
            "psy-rd=2.0",
            "psy-rdoq=1.7",
            "aq-mode=3",
            "aq-strength=0.75",
        ]
        x265_params = ":".join(x265_params_list)

    return [
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
        "-crf", "13",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
        "-x265-params", x265_params,
        str(output),
    ]  # fmt: skip


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

        key, _, val = line.partition("=")
        if key in expected_keys:
            if key == "frame" and val.isdigit():
                ffmpeg_progress.update(int(val) - ffmpeg_progress.n)
        else:
            # Unknown progress key, likely ffmpeg warning or error.
            tqdm.write(line, file=sys.stderr)


def worker(
    file_stem: str,
    output_path: Path,
    x265_params: str,
    queue: multiprocessing.Queue,
) -> None:
    log.info(f"Applying VapourSynth filter: {file_stem}")
    # Max 8 threads for high-end CPUs. Scale down to 1/2 for low-end CPUs to leave room for FFmpeg.
    vs.core.num_threads = min(8, max(1, multiprocessing.cpu_count() // 2))

    if getattr(sys, "frozen", False):
        root = Path(sys.executable).parent
    else:
        root = Path(__file__).parent.parent

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        module_name = file_stem
        if importlib.util.find_spec(f"vs.{file_stem}") is None:
            if file_stem.endswith("_Girl"):
                alt_stem = file_stem.removesuffix("_Girl") + "_Boy"
            elif file_stem.endswith("_Boy"):
                alt_stem = file_stem.removesuffix("_Boy") + "_Girl"
            else:
                alt_stem = None

            if alt_stem and importlib.util.find_spec(f"vs.{alt_stem}") is not None:
                log.info(f"VapourSynth script for {file_stem} not found, using {alt_stem} instead.")
                module_name = alt_stem

        module = importlib.import_module(f"vs.{module_name}")

        filter_chain = getattr(module, "filter_chain", None)
        source = output_path / f"{file_stem}.ivf"
        clip = filter_chain(source)
    except Exception as e:
        log.warning(f"Error importing VapourSynth script for {file_stem}: {e}")
        queue.put(False)
        return

    cmd = ffmpeg_params(
        ffmpeg_path=root / "ffmpeg.exe",
        output=output_path / f"{file_stem}_filtered.mkv",
        custom_x265_params=x265_params,
    )

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        log.error("FFmpeg not found. Place ffmpeg.exe in the root directory and try again.")
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
        # ffmpeg_pipe writes VS frames into ffmpeg's stdin, then parse_ffmpeg_stderr reads ffmpeg's
        # stderr on the main thread. They must run concurrently to keep both pipes continuously
        # drained. If stdin is written without draining stderr, ffmpeg's stderr buffer fills up,
        # ffmpeg stalls, stdin backs up, creating a deadlock.
        def ffmpeg_pipe() -> None:
            try:
                with process.stdin:
                    # Streams encoded Y4M frames into FFmpeg's stdin. clip.output()
                    # blocks until all frames have been written and stdin is closed.
                    clip.output(
                        process.stdin,
                        y4m=True,
                        progress_update=lambda current, _: vapoursynth_progress.update(
                            current - vapoursynth_progress.n
                        ),
                    )
            finally:
                # Prevent early exit leaving the progress bar stuck.
                if vapoursynth_progress.n < total_frames:
                    vapoursynth_progress.update(total_frames - vapoursynth_progress.n)

        with ThreadPoolExecutor(max_workers=1) as executor:
            # ffmpeg_pipe runs on a background thread, continuously writing frames to ffmpeg's stdin
            # while the main thread drains stderr.
            vs_future = executor.submit(ffmpeg_pipe)

            # Blocks until ffmpeg closes stderr (only happens when FFmpeg exits). By then
            # executor's __exit__ joins the background thread, so vs_future is resolved.
            parse_ffmpeg_stderr(process, ffmpeg_progress)

        # FFmpeg exit, collect the exit code.
        return_code = process.wait()

    if exc := vs_future.exception():
        log.error(f"\nVapourSynth processing failed: {exc}")
        queue.put(False)
        return

    if return_code != 0:
        log.error(f"FFmpeg exited with code {return_code}")
        queue.put(False)
        return

    queue.put(True)


def vapoursynth_filter(
    file_stem: str,
    output_path: Path,
    x265_params: str = "",
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
        args=(file_stem, output_path, x265_params, queue),
    )
    process.start()
    process.join()

    if process.exitcode != 0:
        log.error(f"VapourSynth worker exited with code {process.exitcode}")
        return None

    if queue.get() is True:
        return output_path / f"{file_stem}_filtered.mkv"

    return None
