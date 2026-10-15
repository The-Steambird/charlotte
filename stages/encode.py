import importlib
import multiprocessing
import re
import subprocess
import sys
import threading

from concurrent.futures import ThreadPoolExecutor
lazy from multiprocessing.synchronize import Event
lazy from pathlib import Path

lazy import vapoursynth as vs

from utils.ffmpeg import ffmpeg_path
from utils.paths import bundle_root
from utils.reporter import QueueReporter, Reporter, relay_worker
from utils.strings import translate


DEFAULT_CRF = 13.5
DEFAULT_PRESET = "slower"
X265_COLOUR_TAGS = "colorprim=bt709:transfer=bt709:colormatrix=smpte170m"

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


def filter_escape(value: str) -> str:
    """ffmpeg unescapes a `-vf` value twice, first as a filter graph and then as one filter's
    options, and each pass has its own special characters. The first `re.sub` is for the
    inner pass and the second wraps it for the outer one."""
    value = re.sub(r"([\\:'])", r"\\\1", value)
    return re.sub(r"([\\'\[\],;])", r"\\\1", value)


def subtitle_filter(subtitle: Path, fonts: list[Path]) -> str:
    """libass takes a font directory rather than font files. No matrix is set because the `ass`
    filter blends in BT.601 whatever the input is tagged, which matches the source anyway."""
    graph = f"ass=filename={filter_escape(str(subtitle))}"
    if fonts:
        graph += f":fontsdir={filter_escape(str(fonts[0].parent))}"
    return graph


def encode_args(
    crf: float, preset: str, x265_params: str | None = None, video_filter: str | None = None
) -> list[str]:
    """Audio and subtitles are muxed by this same ffmpeg run because the bundled build cannot
    decode HEVC, and remuxing the B-frame stream later would clamp its timestamps."""
    if x265_params is None:
        tuning = [
            "keyint=300",
            "min-keyint=30",
            "no-open-gop=1",
            "aq-mode=3",
            "aq-strength=0.75",
            "qcomp=0.72",
            "cbqpoffs=-2",
            "crqpoffs=-2",
            "no-cutree=1",
            "psy-rd=2.0",
            "psy-rdoq=1.7",
            "no-strong-intra-smoothing=1",
            "deblock=-2,-2",
            "no-sao=1",
            "no-sao-non-deblock=1",
        ]
        # The effort knobs would undo the speed a faster preset was picked for, and ultrafast
        # cannot even open with bframes=8 against its rc-lookahead of 5.
        if preset in ("slow", "slower", "veryslow", "placebo"):
            tuning += ["ref=6", "bframes=8", "lookahead-slices=0", "rd=4", "max-merge=5", "tskip=1"]
        x265_params = ":".join(tuning)

    params = f"{x265_params}:{X265_COLOUR_TAGS}" if x265_params else X265_COLOUR_TAGS
    args = ["-vf", video_filter] if video_filter else []
    args += [
        "-c:v", "libx265",
        "-pix_fmt", "yuv420p10le",
        "-profile:v", "main10",
        "-preset", preset,
        "-crf", str(crf),
        "-color_range", "tv",
        "-x265-params", params,
        "-c:a", "copy",
        "-c:s", "copy",
    ]  # fmt: skip
    return args


def parse_ffmpeg_stderr(process: subprocess.Popen, task, reporter: Reporter) -> None:
    progress_keys = {
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

        key, _, value = line.partition("=")
        if key == "frame" and value.isdigit():
            task.set_completed(int(value))
        elif key not in progress_keys:
            match = LEVEL_TAG.search(line)
            level = FFMPEG_LEVELS[match.group(1)] if match else "info"
            reporter.log(level, line)


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


def build_clip(source: Path, script: str | None, reporter: Reporter):
    if script:
        reporter.log("info", translate("VS_FILTER_APPLYING", script=f"vs/{script}.py"))
        return importlib.import_module(f"vs.{script}").filter_chain(source)
    return vs.core.bs.VideoSource(str(source), showprogress=False)


def kill_on_stop(stop: Event, process: subprocess.Popen) -> None:
    stop.wait()
    process.kill()
    process.wait()


def worker(
    source: Path,
    script: str | None,
    ffmpeg_args: list[str],
    queue: multiprocessing.Queue,
    stop: Event,
) -> None:
    vs.core.num_threads = min(8, max(1, multiprocessing.cpu_count() // 2))

    reporter = QueueReporter(queue)

    root = bundle_root()
    for path in (root / "vs", root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    try:
        clip = build_clip(source, script, reporter)
    except Exception as e:
        reporter.log("warning", translate("VS_CLIP_FAILED", stem=source.stem, error=e))
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

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Closing stdin is not enough to stop ffmpeg on a cancel, because it first flushes the whole
    # x265 lookahead, which takes a few minutes on the slow presets and keeps the audio inputs and
    # the .part open, where the parent's cleanup cannot delete them.
    threading.Thread(target=kill_on_stop, args=(stop, process), daemon=True).start()

    with (
        reporter.task("ffmpeg", total=clip.num_frames, unit="frames") as ffmpeg_task,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        # stderr is drained on its own thread while this one feeds stdin. A full stderr pipe
        # stalls ffmpeg, which then stops reading stdin and deadlocks both. VapourSynth stays on
        # the main thread because of its CUDA and COM contexts.
        stderr_future = executor.submit(parse_ffmpeg_stderr, process, ffmpeg_task, reporter)
        try:
            with process.stdin as stdin:
                clip.output(stdin, y4m=True)
        except Exception as e:
            reporter.log("error", translate("VS_PROCESSING_FAILED", error=e))
            process.kill()
            queue.put(("result", False))
            return
        stderr_future.result()

    return_code = process.wait()

    if return_code != 0:
        reporter.log("error", translate("FFMPEG_EXITED", code=return_code))
        queue.put(("result", False))
        return

    queue.put(("result", True))


def vapoursynth_filter(
    source: Path,
    reporter: Reporter,
    ffmpeg_args: list[str],
    script: str | None = None,
) -> bool:
    """The worker runs in its own process because BestSource keeps the .ivf open for as long as
    the VapourSynth core lives, which is the whole process, and the cleanup would fail to delete it.
    `ffmpeg_args` comes from `stages.mux.mux_args` and holds everything after the Y4M input."""
    queue = multiprocessing.Queue()
    stop = multiprocessing.Event()
    process = multiprocessing.Process(
        target=worker,
        args=(source, script, ffmpeg_args, queue, stop),
    )
    process.start()
    result = relay_worker(reporter, queue, process, stop)
    process.join()

    if process.exitcode != 0:
        reporter.log("error", translate("VS_WORKER_EXITED", code=process.exitcode))
        return False
    return bool(result)
