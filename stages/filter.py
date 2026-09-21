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
X265_COLOUR_TAGS = "colorprim=bt709:transfer=bt709:colormatrix=smpte170m"

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


def encode_args(crf: float, preset: str, x265_params: str = "") -> list[str]:
    """The other tracks are muxed in the same ffmpeg run because the bundled build has no
    HEVC decoder and a later copy of the B-frame stream would come out with clamped
    timestamps. The muxer is named because the output ends in `.part`."""
    if not x265_params:
        x265_params = ":".join(
            [
                "keyint=300",
                "min-keyint=30",
                "no-open-gop=1",
                "ref=6",
                "bframes=8",
                "lookahead-slices=0",
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

    return [
        "-f", "matroska",
        "-c:v", "libx265",
        "-pix_fmt", "yuv420p10le",
        "-profile:v", "main10",
        "-preset", preset,
        "-crf", str(crf),
        "-color_range", "tv",
        "-x265-params", f"{x265_params}:{X265_COLOUR_TAGS}" if x265_params else X265_COLOUR_TAGS,
        "-c:a", "copy",
        "-c:s", "copy",
    ]  # fmt: skip


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
    candidates.append("default")
    for name in candidates:
        if (bundle_root() / "vs" / f"{name}.py").exists():
            return name
    return None


def burn_subtitle(clip, subtitle: Path, fonts: list[Path]):
    """Render an .ass file onto the clip with libass (the subtext plugin)."""
    import vapoursynth as vs

    font_dir = str(fonts[0].parent) if fonts else None
    return vs.core.sub.TextFile(clip, file=str(subtitle), fontdir=font_dir, matrix_s="170m")


def worker(
    source: Path,
    script: str | None,
    subtitle: Path | None,
    fonts: list[Path],
    ffmpeg_args: list[str],
    queue: multiprocessing.Queue,
) -> None:
    import vapoursynth as vs

    vs.core.num_threads = min(8, max(1, multiprocessing.cpu_count() // 2))

    reporter = QueueReporter(queue)

    root = bundle_root()
    for path in (root / "vs", root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    try:
        if script:
            reporter.log("info", f"Applying VapourSynth filter: vs/{script}.py")
            clip = importlib.import_module(f"vs.{script}").filter_chain(source)
        else:
            clip = vs.core.bs.VideoSource(str(source), showprogress=False)
        if subtitle:
            reporter.log("info", f"Burning subtitle: {subtitle.name}")
            clip = burn_subtitle(clip, subtitle, fonts)
    except Exception as e:
        reporter.log("warning", f"Error building the VapourSynth clip for {source.stem}: {e}")
        queue.put(("result", False))
        return

    cmd = [
        str(ffmpeg_path()),
        "-y",
        "-hide_banner",
        "-v", "info",
        "-nostats",
        "-progress", "pipe:2",
        "-f", "yuv4mpegpipe",
        "-i", "pipe:0",
        *ffmpeg_args,
    ]  # fmt: skip

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
                process.kill()
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
    source: Path,
    reporter: Reporter,
    ffmpeg_args: list[str],
    script: str | None = None,
    subtitle: Path | None = None,
    fonts: list[Path] | None = None,
) -> bool:
    """
    Runs the worker in an isolated process. `ffmpeg_args` comes from `stages.mux.mux_args`
    and carries the encode options, the other tracks and the output path.

    vssource.BestSource seems to hold an OS-level file handle to index and read the .ivf
    file. Because VapourSynth's core environment is effectively a global singleton in the
    Python process and caches these indexers, the .ivf file remains locked even after the
    clip goes out of scope and the filter completes, breaking the -nc flag when
    file.unlink() is called.
    """
    ctx = multiprocessing.get_context()
    queue = ctx.Queue()

    process = ctx.Process(
        target=worker,
        args=(source, script, subtitle, fonts or [], ffmpeg_args, queue),
    )
    process.start()
    result = relay_worker(reporter, queue, process)
    process.join()

    if process.exitcode != 0:
        reporter.log("error", f"VapourSynth worker exited with code {process.exitcode}")
        return False
    return bool(result)
