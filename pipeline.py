import shutil

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from resources.keys import find_key_from_file, get_decryption_key
from resources.subtitles import local_subtitle_path
from stages.ass import ASS
from stages.filter import find_vs_script, vapoursynth_filter
from stages.hca import HCA
from stages.mux import mux
from stages.usm import USM
from utils.errors import Cancelled
from utils.ffmpeg import AUDIO_CODECS
from utils.languages import SUBTITLES_LANGUAGES
from utils.logger import log


if TYPE_CHECKING:
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
    x265_params: str
    fonts: tuple[Path, Path] | None = None
    manual_key: int | None = None
    default_audio: str = "ja"
    default_subtitle: str = "EN"
    audio_codec: str = "flac"
    skip_existing: bool = False
    flat: bool = False


def process_audio(
    hca_files: list[Path],
    key1: bytes,
    key2: bytes,
    output_path: Path,
    keep_decrypted: bool,
    codec: str,
) -> list[Path]:
    def convert_one(hca_file: Path) -> Path:
        hca = HCA(hca_file, key1, key2)
        hca.decrypt()
        if keep_decrypted:
            hca.save()
        return hca.convert(output_path=output_path, codec=codec)

    with ThreadPoolExecutor() as executor:
        return list(executor.map(convert_one, hca_files))


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


def cleanup_files(file_paths: dict[str, list[Path]], output_path: Path) -> None:
    for value in file_paths.values():
        for file in value:
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


def process_usm(usm_file: Path, opts: Options, reporter: Reporter) -> None:
    reporter.checkpoint()

    stem = usm_file.stem
    log.info(f"Processing: {usm_file.name}")
    reporter.event("job_start", file=usm_file.name, stem=stem)

    final_mkv = Path(opts.output) / (f"{stem}.mkv" if opts.flat else f"{stem}/{stem}.mkv")
    if opts.skip_existing and final_mkv.exists():
        log.info(f"Skipping {usm_file.name}: output already exists.")
        reporter.event("job_skipped", file=usm_file.name, reason="exists")
        return

    keys = get_decryption_key(usm_file.name, reporter, manual_key=opts.manual_key)
    if keys is None:
        log.warning(f"Could not find decryption keys for {usm_file.name}, skipping...")
        reporter.event("job_skipped", file=usm_file.name, reason="no_key")
        return
    reporter.checkpoint()  # also catches a cancel that arrived during ask()

    key1, key2 = keys
    usm = USM(usm_file, key1, key2)
    output_path = Path(opts.output) / f"{stem}"
    output_path.mkdir(exist_ok=True)
    file_paths = usm.demux(output_path=output_path, reporter=reporter)

    try:
        reporter.checkpoint()

        hca_files = file_paths.get("hca", [])
        audio_files = process_audio(
            hca_files,
            key1,
            key2,
            output_path,
            keep_decrypted=opts.no_cleanup,
            codec=opts.audio_codec,
        )
        file_paths.setdefault("audio", []).extend(audio_files)

        ass_files = process_subtitles(
            stem=BASENAME_FIXES.get(stem, stem),
            output_path=output_path,
        )
        file_paths.setdefault("ass", []).extend(ass_files)

        reporter.checkpoint()

        filtered_mkv: Path | None = None
        if opts.vapoursynth:
            filtered_mkv = vapoursynth_filter(
                file_stem=stem,
                output_path=output_path,
                reporter=reporter,
                crf=opts.crf,
                preset=opts.preset,
                x265_params=opts.x265_params,
            )
            if filtered_mkv:
                file_paths.setdefault("vs", []).append(filtered_mkv)
            else:
                log.warning(f"Failed to apply VapourSynth filter for {stem}, skipping...")

        reporter.checkpoint()

        mux(
            output_path,
            vs_path=filtered_mkv,
            fonts=opts.fonts,
            default_audio=opts.default_audio,
            default_subtitle=opts.default_subtitle,
            audio_extension=AUDIO_CODECS.get(opts.audio_codec, AUDIO_CODECS["flac"])[0],
        )
    except Cancelled:
        if not opts.no_cleanup:
            cleanup_files(file_paths, output_path)
        raise

    if opts.flat:
        final_mkv.parent.mkdir(parents=True, exist_ok=True)
        (output_path / f"{stem}.mkv").replace(final_mkv)
        if not opts.no_cleanup:
            shutil.rmtree(output_path, ignore_errors=True)
    elif not opts.no_cleanup:
        cleanup_files(file_paths, output_path)

    reporter.event(
        "result",
        file=usm_file.name,
        stem=stem,
        output=str(final_mkv),
        status="ok",
    )


def probe_usm(usm_file: Path, keys_data: dict, reporter: Reporter) -> None:
    """Report what is available for this file without processing anything: no
    downloads, no prompts, no writes."""
    stem = usm_file.stem
    sub_stem = BASENAME_FIXES.get(stem, stem)
    key = find_key_from_file(keys_data, stem) is not None
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
        subtitles=subtitles,
        vs_script=vs_script,
    )
