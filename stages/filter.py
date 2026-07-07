import importlib
import multiprocessing
import re
import subprocess
import sys

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from utils.ffmpeg import FFMPEG_MISSING, ffmpeg_path
from utils.paths import bundle_root
from utils.reporter import QueueReporter, Reporter, relay_worker


if TYPE_CHECKING:
    from pathlib import Path


DEFAULT_CRF = 13.5
DEFAULT_PRESET = "slower"

# Map ffmpeg log level to internal levels (error/warning/info/debug).
FFMPEG_LEVELS = {
    "panic": "error",
    "fatal": "error",
    "error": "error",
    "warning": "warning",
    "info": "info",
    "verbose": "debug",
    "debug": "debug",
    "trace": "debug",
}
LEVEL_TAG = re.compile(r"\[(panic|fatal|error|warning|info|verbose|debug|trace)]")


def ffmpeg_params(
    output: Path,
    crf: float,
    preset: str,
    x265_params: str = "",
) -> list[str]:
    if not x265_params and crf == DEFAULT_CRF and preset == DEFAULT_PRESET:
        x265_params = ":".join(
            [
                "keyint=300",
                "min-keyint=30",
                "no-open-gop=1",
                "ref=6",
                "bframes=8",
                "lookahead-slices=0",
                "rc-lookahead=60",
                "aq-mode=3",
                "aq-strength=0.75",
                "qcomp=0.72",
                "cbqpoffs=-2",
                "crqpoffs=-2",
                "no-cutree=1",
                "rd=4",
                "psy-rd=2.0",
                "psy-rdoq=1.7",
                "max-merge=5",
                "no-strong-intra-smoothing=1",
                "tskip=1",
                "deblock=-2,-2",
                "no-sao=1",
                "no-sao-non-deblock=1",
            ]
        )

    cmd = [
        str(ffmpeg_path()),
        "-y",
        "-hide_banner",
        "-v", "info",
        "-nostats",
        "-progress", "pipe:2",
        "-f", "yuv4mpegpipe",
        "-i", "pipe:0",
        "-c:v", "libx265",
        "-pix_fmt", "yuv420p10le",
        "-profile:v", "main10",
        "-preset", preset,
        "-crf", str(crf),
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
    ]  # fmt: skip
    if x265_params:
        cmd += ["-x265-params", x265_params]
    cmd.append(str(output))
    return cmd


def parse_ffmpeg_stderr(process: subprocess.Popen, ffmpeg_task, reporter: Reporter) -> None:
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
                ffmpeg_task.set_completed(int(val))
        else:
            match = LEVEL_TAG.search(line)
            reporter.log(FFMPEG_LEVELS[match.group(1)] if match else "info", line)


def find_vs_script(stem: str) -> str | None:
    candidates = [stem]
    if stem.endswith("_Girl"):
        candidates.append(stem.removesuffix("_Girl") + "_Boy")
    elif stem.endswith("_Boy"):
        candidates.append(stem.removesuffix("_Boy") + "_Girl")
    for name in candidates:
        if (bundle_root() / "vs" / f"{name}.py").exists():
            return name
    return None


def worker(
    file_stem: str,
    output_path: Path,
    crf: float,
    preset: str,
    x265_params: str,
    queue: multiprocessing.Queue,
) -> None:
    import vapoursynth as vs

    vs.core.num_threads = min(8, max(1, multiprocessing.cpu_count() // 2))

    reporter = QueueReporter(queue)
    reporter.log("info", f"Applying VapourSynth filter: {file_stem}")

    root = bundle_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        module_name = find_vs_script(file_stem) or file_stem
        if module_name != file_stem:
            reporter.log(
                "info",
                f"VapourSynth script for {file_stem} not found, using {module_name} instead.",
            )

        module = importlib.import_module(f"vs.{module_name}")
        source = output_path / f"{file_stem}.ivf"
        clip = module.filter_chain(source)
    except Exception as e:
        reporter.log("warning", f"Error importing VapourSynth script for {file_stem}: {e}")
        queue.put(("result", False))
        return

    cmd = ffmpeg_params(
        output=output_path / f"{file_stem}_filtered.mkv",
        crf=crf,
        preset=preset,
        x265_params=x265_params,
    )

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        reporter.log("error", FFMPEG_MISSING)
        queue.put(("result", False))
        return

    total_frames = clip.num_frames

    with reporter.task("ffmpeg", total=total_frames, unit="frames") as ffmpeg_task:
        # ffmpeg_pipe writes VS frames into ffmpeg's stdin, then parse_ffmpeg_stderr reads ffmpeg's
        # stderr on the main thread. They must run concurrently to keep both pipes continuously
        # drained. If stdin is written without draining stderr, ffmpeg's stderr buffer fills up,
        # ffmpeg stalls, stdin backs up, creating a deadlock.
        def ffmpeg_pipe() -> None:
            with process.stdin as stdin:
                clip.output(stdin, y4m=True)

        with ThreadPoolExecutor(max_workers=1) as executor:
            # parse_ffmpeg_stderr runs on a background thread, continuously reading ffmpeg's stderr
            # while the main thread safely handles VapourSynth (CUDA/COM contexts).
            stderr_future = executor.submit(parse_ffmpeg_stderr, process, ffmpeg_task, reporter)

            # Blocks until VapourSynth finishes writing all frames.
            try:
                ffmpeg_pipe()
            except Exception as e:
                reporter.log("error", f"\nVapourSynth processing failed: {e}")
                queue.put(("result", False))
                return

            stderr_future.result()

        return_code = process.wait()

    if return_code != 0:
        reporter.log("error", f"FFmpeg exited with code {return_code}")
        queue.put(("result", False))
        return

    queue.put(("result", True))


def vapoursynth_filter(
    file_stem: str,
    output_path: Path,
    reporter: Reporter,
    crf: float,
    preset: str,
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
        args=(file_stem, output_path, crf, preset, x265_params, queue),
    )
    process.start()
    result = relay_worker(reporter, queue, process)
    process.join()

    if process.exitcode != 0:
        reporter.log("error", f"VapourSynth worker exited with code {process.exitcode}")
        return None
    if result is True:
        return output_path / f"{file_stem}_filtered.mkv"
    return None
