import shutil

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from resources.keys import calculate_key_from_filename, find_video_version
from resources.subtitles import local_subtitle_path
from stages.ass import ASS
from stages.crack import crack_key
from stages.filter import encode_args, find_vs_script, subtitle_filter, vapoursynth_filter
from stages.hca import HCA
from stages.mux import mux, mux_args, track_code
from stages.usm import USM
from utils.errors import Cancelled, CharlotteError, Skipped
from utils.ffmpeg import AUDIO_CODECS
from utils.languages import SUBTITLES_LANGUAGES
from utils.logger import log


if TYPE_CHECKING:
    from resources.keys import Keys
    from stages.crack import Recovery
    from utils.reporter import Reporter


BASENAME_FIXES = {
    "Cs_4131904_HaiDaoChuXian_Boy": "Cs_Activity_4001103_Summertime_Boy",
    "Cs_4131904_HaiDaoChuXian_Girl": "Cs_Activity_4001103_Summertime_Girl",
    "Cs_200211_WanYeXianVideo": "Cs_DQAQ200211_WanYeXianVideo",
}


@dataclass(frozen=True)
class Options:
    output: str
    no_cleanup: bool
    vapoursynth: bool
    crf: float
    preset: str
    x265_params: str | None
    fonts: list[Path] | None = None
    default_audio: str = "ja"
    default_subtitle: str = "EN"
    audio_codec: str = "flac"
    skip_existing: bool = False
    flat: bool = False
    hard_sub: bool = False


def process_audio(
    hca_files: list[Path],
    audio_files: list[Path],
    key1: bytes,
    key2: bytes,
    keep_decrypted: bool,
    codec: str,
) -> None:
    def convert_one(hca_file: Path, audio_file: Path) -> None:
        hca = HCA(hca_file, key1, key2)
        hca.decrypt()
        if keep_decrypted:
            hca.save()
        hca.convert(audio_file, codec)

    with ThreadPoolExecutor() as executor:
        list(executor.map(convert_one, hca_files, audio_files))


def process_subtitles(stem: str, output_path: Path) -> list[Path]:
    subtitle_files = []
    for lang in SUBTITLES_LANGUAGES:
        sub_path = local_subtitle_path(stem, lang)
        if sub_path.exists():
            subtitle_files.append((sub_path, lang))

    log.info(f"Found {len(subtitle_files)} subtitle file(s).")

    ass_files = []
    empty_langs = []
    for sub_file, lang in subtitle_files:
        try:
            ass = ASS(sub_file, lang)
            if ass.parse_srt():
                ass_files.append(ass.convert_to_ass(output_path=output_path))
            elif sub_file.stat().st_size == 0:
                empty_langs.append(SUBTITLES_LANGUAGES[lang][1])
        except Exception as e:
            log.error(f"Error processing subtitle: {e}")

    if empty_langs:
        log.info(f"Subtitles empty, skipping: {', '.join(empty_langs)}")

    return ass_files


def encode_video(
    video: Path,
    partial_mkv: Path,
    file_paths: dict[str, list[Path]],
    opts: Options,
    reporter: Reporter,
) -> bool:
    stem = video.stem
    script = find_vs_script(stem) if opts.vapoursynth else None
    if opts.vapoursynth and script is None:
        log.warning(f"No VapourSynth script found for {stem}, skipping filter...")
    elif script and script != stem:
        log.info(f"VapourSynth script for {stem} not found, using {script} instead.")

    burnt_subtitle = None
    if opts.hard_sub:
        burnt_subtitle = next(
            (path for path in file_paths["ass"] if track_code(path) == opts.default_subtitle), None
        )
        if burnt_subtitle is None:
            log.warning(f"No {opts.default_subtitle} subtitle for {stem}, nothing to burn in.")
        else:
            log.info(f"Burning subtitle: {burnt_subtitle.name}")

    if not (script or burnt_subtitle):
        return False

    video_filter = subtitle_filter(burnt_subtitle, opts.fonts) if burnt_subtitle else None
    ffmpeg_args = mux_args(
        partial_mkv,
        encode_args(opts.crf, opts.preset, opts.x265_params, video_filter),
        file_paths["audio"],
        [] if burnt_subtitle else file_paths["ass"],
        fonts=opts.fonts,
        default_audio=opts.default_audio,
        default_subtitle=opts.default_subtitle,
    )
    if vapoursynth_filter(source=video, reporter=reporter, ffmpeg_args=ffmpeg_args, script=script):
        return True
    log.warning(f"Encode failed, falling back to a lossless mux: {stem}")
    return False


def cleanup_files(file_paths: dict[str, list[Path]], output_path: Path) -> None:
    for files in file_paths.values():
        for file in files:
            try:
                file.unlink(missing_ok=True)
            except OSError as e:
                log.error(f"Failed to delete {file.name}: {e}")

    subs_dir = output_path / "subs"
    try:
        if subs_dir.is_dir():
            shutil.rmtree(subs_dir)
    except OSError as e:
        log.error(f"Failed to remove directory {subs_dir.name}: {e}")

    with suppress(OSError):
        output_path.rmdir()


def process_usm(usm_file: Path, opts: Options, reporter: Reporter, keys: Keys) -> None:
    reporter.checkpoint()

    stem = usm_file.stem
    log.info(f"Processing: {usm_file.name}")
    reporter.event("job_start", file=usm_file.name, stem=stem)

    final_mkv = Path(opts.output) / (f"{stem}.mkv" if opts.flat else f"{stem}/{stem}.mkv")
    if opts.skip_existing and final_mkv.exists():
        log.info(f"Skipping {usm_file.name}: output already exists.")
        reporter.event("job_skipped", file=usm_file.name, reason="exists")
        return

    key_pair = keys.decryption_key(stem)
    if key_pair is None:
        # Attempt to find key from USM files directly.
        key_pair = crack_usm(usm_file, reporter).key
        if key_pair is None:
            log.warning(f"Could not find decryption keys for {usm_file.name}, skipping...")
            reporter.event("job_skipped", file=usm_file.name, reason="no_key")
            return
    reporter.checkpoint()

    key1, key2 = key_pair
    usm = USM(usm_file, key1, key2)
    output_path = Path(opts.output) / stem
    output_path.mkdir(exist_ok=True)
    video = output_path / f"{stem}.ivf"
    # Both the encode and the mux write here, and only a finished run renames it. A killed
    # ffmpeg can leave a valid looking truncated file where --skip-existing would mistake as
    # a finished one.
    partial_mkv = output_path / f"{stem}.mkv.part"
    file_paths: dict[str, list[Path]] = {"mkv": [partial_mkv]}
    try:
        usm.demux(output_path=output_path, reporter=reporter, file_paths=file_paths)
        reporter.checkpoint()

        hca_files = file_paths.get("hca", [])
        extension = AUDIO_CODECS[opts.audio_codec][0]
        file_paths["audio"] = [hca_file.with_suffix(extension) for hca_file in hca_files]
        process_audio(
            hca_files,
            file_paths["audio"],
            key1,
            key2,
            keep_decrypted=opts.no_cleanup,
            codec=opts.audio_codec,
        )
        file_paths["ass"] = process_subtitles(
            stem=BASENAME_FIXES.get(stem, stem),
            output_path=output_path,
        )
        reporter.checkpoint()

        if not encode_video(video, partial_mkv, file_paths, opts, reporter):
            mux(
                video,
                partial_mkv,
                file_paths["audio"],
                file_paths["ass"],
                fonts=opts.fonts,
                default_audio=opts.default_audio,
                default_subtitle=opts.default_subtitle,
            )
        reporter.checkpoint()
    except Cancelled, Skipped, CharlotteError:
        if not opts.no_cleanup:
            cleanup_files(file_paths, output_path)
        raise

    try:
        partial_mkv.replace(final_mkv)
    except OSError as e:
        raise CharlotteError(f"Failed to move {partial_mkv.name} into place: {e}") from e
    log.info(f"Created: {final_mkv}")

    if not opts.no_cleanup:
        cleanup_files(file_paths, output_path)

    reporter.event(
        "result",
        file=usm_file.name,
        stem=stem,
        output=str(final_mkv),
        status="ok",
    )


def crack_usm(usm_file: Path, reporter: Reporter) -> Recovery:
    stem = usm_file.stem
    recovery = crack_key(usm_file, reporter)

    key_pair = recovery.key
    combined = video_key = None
    if key_pair is not None:
        key1, key2 = key_pair
        combined = int.from_bytes(key1 + key2, "little")
        video_key = (combined - calculate_key_from_filename(stem)) & 0xFFFFFFFFFFFFFF
        log.info(f"{usm_file.name}: videoKey={video_key}")

    reporter.event(
        "crack",
        file=usm_file.name,
        stem=stem,
        key=combined,
        video_key=video_key,
        reason=recovery.reason,
    )
    return recovery


def crack_all(usm_files: list[Path], reporter: Reporter) -> None:
    failures: dict[str, str] = {}  # filename -> why its key could not be recovered
    for usm_file in usm_files:
        reporter.event("job_start", file=usm_file.name, stem=usm_file.stem)
        try:
            recovery = crack_usm(usm_file, reporter)
        except Cancelled:
            log.info(f"Cancelled during {usm_file.name}.")
            reporter.event("cancelled", file=usm_file.name)
            return
        except Skipped:
            log.info(f"Skipped {usm_file.name} on request.")
            reporter.event("job_skipped", file=usm_file.name, reason="requested")
            failures[usm_file.name] = "skipped"
            continue
        except CharlotteError as e:
            log.error(f"Failed to read {usm_file.name}: {e}")
            reporter.event("error", file=usm_file.name, message=str(e))
            failures[usm_file.name] = str(e)
            continue

        if recovery.key is None:
            failures[usm_file.name] = recovery.reason

    recovered = len(usm_files) - len(failures)
    log.info(f"Recovered {recovered} of {len(usm_files)} key(s).")
    if failures:
        log.warning(f"{len(failures)} file(s) need a key from another source:")
        for name, reason in failures.items():
            log.warning(f"  {name}: {reason}")

    reporter.event("crack_summary", recovered=recovered, unrecovered=len(failures))


def probe_usm(usm_file: Path, keys: Keys, reporter: Reporter) -> None:
    stem = usm_file.stem
    sub_stem = BASENAME_FIXES.get(stem, stem)
    key = keys.get(stem) is not None
    version = find_video_version(keys.data, stem)
    subtitles = [
        lang for lang in SUBTITLES_LANGUAGES if local_subtitle_path(sub_stem, lang).exists()
    ]
    vs_script = find_vs_script(stem)

    level = log.info if key else log.warning
    level(
        f"{usm_file.name}: key={'yes' if key else 'MISSING'}, "
        f"subtitles={','.join(subtitles) or 'none'}, vs={vs_script or 'none'}"
    )
    reporter.event(
        "probe",
        file=usm_file.name,
        stem=stem,
        key=key,
        version=version,
        subtitles=subtitles,
        vs_script=vs_script,
    )
