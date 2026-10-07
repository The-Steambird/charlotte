import shutil

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from resources.keys import KEY_MASK, calculate_key_from_filename, find_video_version
from resources.subtitles import local_subtitle_path
from stages.ass import ASS
from stages.crack import crack_key
from stages.filter import encode_args, find_vs_script, subtitle_filter, vapoursynth_filter
from stages.hca import HCA
from stages.mux import mux, mux_args, subset_fonts, track_code
from stages.usm import USM, video_nonce
from utils.errors import Cancelled, CharlotteError, Skipped
from utils.ffmpeg import AUDIO_CODECS
from utils.languages import SUBTITLES_LANGUAGES
from utils.logger import log
from utils.strings import translate


if TYPE_CHECKING:
    from pathlib import Path

    from resources.keys import DecryptionKey, Keys
    from stages.crack import Recovery
    from utils.reporter import Reporter


BASENAME_FIXES = {
    "Cs_4131904_HaiDaoChuXian_Boy": "Cs_Activity_4001103_Summertime_Boy",
    "Cs_4131904_HaiDaoChuXian_Girl": "Cs_Activity_4001103_Summertime_Girl",
    "Cs_200211_WanYeXianVideo": "Cs_DQAQ200211_WanYeXianVideo",
}


@dataclass(frozen=True)
class Options:
    output: Path
    no_cleanup: bool
    vapoursynth: bool
    crf: float
    preset: str
    x265_params: str | None
    fonts: list[Path]
    default_audio: str
    default_subtitle: str
    audio_codec: str
    skip_existing: bool
    flat: bool
    hard_sub: bool


def process_audio(
    hca_files: list[Path],
    audio_files: list[Path],
    key: DecryptionKey,
    keep_decrypted: bool,
    codec: str,
) -> None:
    def convert_one(hca_file: Path, audio_file: Path) -> None:
        hca = HCA(hca_file, key)
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

    log.info(translate("FOUND_SUBTITLES", count=len(subtitle_files)))

    ass_files = []
    empty_langs = []
    for sub_file, lang in subtitle_files:
        try:
            ass = ASS(sub_file, lang)
            if ass.parse_srt():
                ass_files.append(ass.convert_to_ass(output_path=output_path))
            elif sub_file.stat().st_size == 0:
                empty_langs.append(SUBTITLES_LANGUAGES[lang][1])
        except (OSError, UnicodeDecodeError) as e:
            log.error(translate("SUBTITLE_CONVERT_FAILED", name=sub_file.name, error=e))

    if empty_langs:
        log.info(translate("SUBTITLES_EMPTY", languages=", ".join(empty_langs)))

    return ass_files


def prepare_subtitles(
    stem: str, output_path: Path, fonts: list[Path]
) -> tuple[list[Path], list[Path]]:
    subtitle_files = process_subtitles(BASENAME_FIXES.get(stem, stem), output_path)
    subsets = subset_fonts(fonts, subtitle_files, output_path / "subs") if subtitle_files else []
    return subtitle_files, subsets


def encode_video(
    video: Path,
    partial_mkv: Path,
    audio_files: list[Path],
    subtitle_files: list[Path],
    fonts: list[Path],
    opts: Options,
    reporter: Reporter,
) -> bool:
    stem = video.stem
    script = find_vs_script(stem) if opts.vapoursynth else None
    if opts.vapoursynth and script is None:
        log.warning(translate("VS_SCRIPT_MISSING", stem=stem))
    elif script and script != stem:
        log.info(translate("VS_SCRIPT_FALLBACK", stem=stem, script=script))

    burnt_subtitle = None
    if opts.hard_sub:
        burnt_subtitle = next(
            (path for path in subtitle_files if track_code(path) == opts.default_subtitle), None
        )
        if burnt_subtitle is None:
            log.warning(translate("HARD_SUB_MISSING", language=opts.default_subtitle, stem=stem))
        else:
            log.info(translate("HARD_SUB_BURNING", name=burnt_subtitle.name))

    if not (script or burnt_subtitle):
        return False

    video_filter = subtitle_filter(burnt_subtitle, opts.fonts) if burnt_subtitle else None
    ffmpeg_args = mux_args(
        partial_mkv,
        encode_args(opts.crf, opts.preset, opts.x265_params, video_filter),
        audio_files,
        [] if burnt_subtitle else subtitle_files,
        fonts=fonts,
        default_audio=opts.default_audio,
        default_subtitle=opts.default_subtitle,
    )
    if vapoursynth_filter(source=video, reporter=reporter, ffmpeg_args=ffmpeg_args, script=script):
        return True
    log.warning(translate("ENCODE_FALLBACK", stem=stem))
    return False


def find_keys(
    usm_file: Path, nonce: int | None, keys: Keys, reporter: Reporter
) -> DecryptionKey | None:
    stem = usm_file.stem
    if nonce is None:
        return keys.decryption_key(stem) or crack_usm(usm_file, reporter).key
    return keys.stream_keys(stem)


def cleanup_files(created: list[Path], output_path: Path) -> None:
    for file in created:
        try:
            file.unlink(missing_ok=True)
        except OSError as e:
            log.error(translate("DELETE_FAILED", name=file.name, error=e))

    subs_dir = output_path / "subs"
    try:
        if subs_dir.is_dir():
            shutil.rmtree(subs_dir)
    except OSError as e:
        log.error(translate("REMOVE_DIRECTORY_FAILED", name=subs_dir.name, error=e))

    with suppress(OSError):
        output_path.rmdir()


def process_usm(usm_file: Path, opts: Options, keys: Keys, reporter: Reporter) -> None:
    reporter.checkpoint()

    stem = usm_file.stem
    log.info(translate("PROCESSING", name=usm_file.name))
    reporter.event("job_start", file=usm_file.name, stem=stem)

    final_mkv = opts.output / (f"{stem}.mkv" if opts.flat else f"{stem}/{stem}.mkv")
    if opts.skip_existing and final_mkv.exists():
        log.info(translate("SKIP_EXISTING_OUTPUT", name=usm_file.name))
        reporter.event("job_skipped", file=usm_file.name, reason="exists")
        return

    nonce = video_nonce(usm_file)
    key = find_keys(usm_file, nonce, keys, reporter)
    if key is None:
        log.warning(translate("NO_KEYS_SKIPPING", name=usm_file.name))
        reporter.event("job_skipped", file=usm_file.name, reason="no_key")
        return
    reporter.checkpoint()

    usm = USM(usm_file, key, nonce)
    output_path = opts.output / stem
    output_path.mkdir(exist_ok=True)
    video = output_path / f"{stem}.ivf"
    # A killed ffmpeg can leave a broken file that --skip-existing would mistake for a finished one.
    partial_mkv = output_path / f"{stem}.mkv.part"
    created = [partial_mkv]
    try:
        hca_files = usm.demux(output_path=output_path, reporter=reporter, created=created)
        reporter.checkpoint()

        extension = AUDIO_CODECS[opts.audio_codec][0]
        audio_files = [hca_file.with_suffix(extension) for hca_file in hca_files]
        created += audio_files
        # Subtitles run during audio conversion as it mostly waits on ffmpeg. During the demux, font
        # subsetting would hold the GIL and slow the chunk walk.
        with ThreadPoolExecutor(max_workers=1) as executor:
            subtitles = executor.submit(prepare_subtitles, stem, output_path, opts.fonts)
            process_audio(
                hca_files,
                audio_files,
                key,
                keep_decrypted=opts.no_cleanup,
                codec=opts.audio_codec,
            )
        subtitle_files, fonts = subtitles.result()
        created += subtitle_files
        reporter.checkpoint()

        if not encode_video(video, partial_mkv, audio_files, subtitle_files, fonts, opts, reporter):
            mux(
                video,
                partial_mkv,
                audio_files,
                subtitle_files,
                fonts=fonts,
                default_audio=opts.default_audio,
                default_subtitle=opts.default_subtitle,
            )
        reporter.checkpoint()
    except Cancelled, Skipped, CharlotteError, OSError:
        if not opts.no_cleanup:
            cleanup_files(created, output_path)
        raise

    partial_mkv.replace(final_mkv)
    log.info(translate("CREATED", path=final_mkv))

    if not opts.no_cleanup:
        cleanup_files(created, output_path)

    reporter.event(
        "result",
        file=usm_file.name,
        stem=stem,
        output=str(final_mkv),
        status="ok",
    )


def process_all(usm_files: list[Path], opts: Options, keys: Keys, reporter: Reporter) -> int:
    failures = 0
    for usm_file in usm_files:
        try:
            process_usm(usm_file, opts, keys, reporter)
        except Cancelled:
            log.info(translate("CANCELLED_DURING", name=usm_file.name))
            reporter.event("cancelled", file=usm_file.name)
            break
        except Skipped:
            log.info(translate("SKIPPED_ON_REQUEST", name=usm_file.name))
            reporter.event("job_skipped", file=usm_file.name, reason="requested")
        except (CharlotteError, OSError) as e:
            log.error(translate("PROCESS_FAILED", name=usm_file.name, error=e))
            reporter.event("error", file=usm_file.name, message=str(e))
            failures += 1
    return failures


def crack_usm(usm_file: Path, reporter: Reporter) -> Recovery:
    stem = usm_file.stem
    recovery = crack_key(usm_file, reporter)

    key = recovery.key
    combined = video_key = None
    if key is not None:
        combined = int.from_bytes(key.key1 + key.key2, "little")
        video_key = (combined - calculate_key_from_filename(stem)) & KEY_MASK
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
            log.info(translate("CANCELLED_DURING", name=usm_file.name))
            reporter.event("cancelled", file=usm_file.name)
            return
        except Skipped:
            log.info(translate("SKIPPED_ON_REQUEST", name=usm_file.name))
            reporter.event("job_skipped", file=usm_file.name, reason="requested")
            failures[usm_file.name] = translate("CRACK_SKIPPED")
            continue
        except (CharlotteError, OSError) as e:
            log.error(translate("READ_FAILED", name=usm_file.name, error=e))
            reporter.event("error", file=usm_file.name, message=str(e))
            failures[usm_file.name] = str(e)
            continue

        if recovery.key is None:
            failures[usm_file.name] = recovery.reason

    recovered = len(usm_files) - len(failures)
    log.info(translate("CRACK_RECOVERED_COUNT", recovered=recovered, total=len(usm_files)))
    if failures:
        log.warning(translate("CRACK_NEED_OTHER_SOURCE", count=len(failures)))
        for name, reason in failures.items():
            log.warning(f"  {name}: {reason}")

    reporter.event("crack_summary", recovered=recovered, unrecovered=len(failures))


def probe_usm(usm_file: Path, keys: Keys, reporter: Reporter) -> None:
    stem = usm_file.stem
    sub_stem = BASENAME_FIXES.get(stem, stem)
    stream_cipher = video_nonce(usm_file) is not None
    key = (keys.stream_keys(stem) if stream_cipher else keys.get(stem)) is not None
    version = find_video_version(keys.data, stem)
    subtitles = [
        lang for lang in SUBTITLES_LANGUAGES if local_subtitle_path(sub_stem, lang).exists()
    ]
    vs_script = find_vs_script(stem)

    level = log.info if key else log.warning
    level(
        translate(
            "PROBE_RESULT",
            name=usm_file.name,
            key=translate("PROBE_KEY_FOUND") if key else translate("PROBE_KEY_MISSING"),
            subtitles=",".join(subtitles) or translate("PROBE_NONE"),
            script=vs_script or translate("PROBE_NONE"),
        )
    )
    reporter.event(
        "probe",
        file=usm_file.name,
        stem=stem,
        key=key,
        version=version,
        subtitles=subtitles,
        vs_script=vs_script,
        stream_cipher=stream_cipher,
    )


def probe_all(usm_files: list[Path], keys: Keys, reporter: Reporter) -> None:
    for usm_file in usm_files:
        try:
            probe_usm(usm_file, keys, reporter)
        except (CharlotteError, OSError) as e:
            log.error(translate("READ_FAILED", name=usm_file.name, error=e))
            reporter.event("error", file=usm_file.name, message=str(e))
