import shutil

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from decoders.ass import ASS
from decoders.hca import HCA
from decoders.usm import USM
from utils.errors import Cancelled
from utils.filter import find_vs_script, vapoursynth_filter
from utils.keys import find_key_from_file, get_decryption_key
from utils.languages import SUBTITLES_LANGUAGES
from utils.logger import log
from utils.mux import mux
from utils.subtitles import get_subtitle_path, local_subtitle_path


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
    crf: float | None
    preset: str | None
    x265_params: str
    fonts: tuple[Path, Path] | None = None


def process_audio(
    hca_files: list[Path], key1: bytes, key2: bytes, output_path: Path, keep_decrypted: bool
) -> list[Path]:
    def convert_one(hca_file: Path) -> Path:
        hca = HCA(hca_file, key1, key2)
        hca.decrypt()
        if keep_decrypted:
            hca.save()
        return hca.convert_to_flac(output_path=output_path)

    with ThreadPoolExecutor() as executor:
        return list(executor.map(convert_one, hca_files))


def process_subtitles(stem: str, output_path: Path) -> list[Path]:
    subtitle_files = []
    for lang in SUBTITLES_LANGUAGES:
        sub_path = get_subtitle_path(stem, lang)
        if sub_path:
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

    keys = get_decryption_key(usm_file.name, reporter)
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

    # Cancel is only observed at stage boundaries here (and continuously inside
    # vapoursynth_filter); on cancel, drop this job's intermediates and bail.
    try:
        reporter.checkpoint()

        hca_files = file_paths.get("hca", [])
        flac_files = process_audio(
            hca_files, key1, key2, output_path, keep_decrypted=opts.no_cleanup
        )
        file_paths.setdefault("flac", []).extend(flac_files)

        ass_files = process_subtitles(BASENAME_FIXES.get(stem, stem), output_path)
        file_paths.setdefault("ass", []).extend(ass_files)

        reporter.checkpoint()

        filtered_mkv: Path | None = None
        if opts.vapoursynth:
            filtered_mkv = vapoursynth_filter(
                file_stem=stem,
                output_path=output_path,
                reporter=reporter,
                custom_crf=opts.crf,
                custom_preset=opts.preset,
                custom_x265_params=opts.x265_params,
            )
            if filtered_mkv:
                file_paths.setdefault("vs", []).append(filtered_mkv)
            else:
                log.warning(f"Failed to apply VapourSynth filter for {stem}, skipping...")

        reporter.checkpoint()

        mux(output_path, vs_path=filtered_mkv, fonts=opts.fonts)
    except Cancelled:
        # A partial {stem}_filtered.mkv may survive this: an orphaned ffmpeg can
        # still hold it. The GUI's Job Object reaps that ffmpeg on exit.
        if not opts.no_cleanup:
            cleanup_files(file_paths, output_path)
        raise

    if not opts.no_cleanup:
        cleanup_files(file_paths, output_path)

    reporter.event(
        "result",
        file=usm_file.name,
        stem=stem,
        output=str(output_path / f"{stem}.mkv"),
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
