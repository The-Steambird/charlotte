import multiprocessing

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated

import typer

from decoders.ass import ASS
from decoders.hca import HCA
from decoders.usm import USM
from utils.filter import vapoursynth_filter
from utils.fonts import fetch_font
from utils.keys import get_decryption_key
from utils.languages import SUBTITLES_LANGUAGES
from utils.logger import log
from utils.mux import mux
from utils.subtitles import get_subtitle_path


app = typer.Typer(help="USM video file demuxer and converter")


def collect_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".usm":
            log.error("Error: File must have a .usm extension")
            raise typer.Exit(1)
        return [input_path]

    if input_path.is_dir():
        files = list(input_path.glob("*.usm"))
        if not files:
            log.error("No .usm files found in directory")
            raise typer.Exit(1)
        return files

    log.error(f"Error: {input_path} is not a valid file or directory")
    raise typer.Exit(1)


def get_fixed_stem(stem: str) -> str:
    basename_fixes = {
        "Cs_4131904_HaiDaoChuXian_Boy": "Cs_Activity_4001103_Summertime_Boy",
        "Cs_4131904_HaiDaoChuXian_Girl": "Cs_Activity_4001103_Summertime_Girl",
        "Cs_200211_WanYeXianVideo": "Cs_DQAQ200211_WanYeXianVideo",
    }
    return basename_fixes.get(stem, stem)


def process_audio(hca_files: list[Path], key1: bytes, key2: bytes, output_path: Path) -> list[Path]:
    def convert_one(hca_file: Path) -> Path:
        hca = HCA(hca_file, key1, key2)
        hca.decrypt()
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
    for sub_file, lang in subtitle_files:
        try:
            ass = ASS(sub_file, lang)
            if ass.parse_srt():
                ass_files.append(ass.convert_to_ass(output_path=output_path))
        except Exception as e:
            log.error(f"Error processing subtitle: {e}")

    return ass_files


def cleanup_files(file_paths: dict[str, list[Path]], output_path: Path) -> None:
    import shutil

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


def process_usm(
    usm_file: Path,
    output: str,
    no_cleanup: bool,
    vapoursynth: bool,
    x265_params: str,
    fonts: tuple[Path, Path] | None = None,
) -> None:
    stem = get_fixed_stem(usm_file.stem)
    log.info(f"Processing: {usm_file.name}")

    keys = get_decryption_key(usm_file.name)
    if keys is None:
        log.warning(f"Could not find decryption keys for {usm_file.name}, skipping...")
        return

    key1, key2 = keys
    usm = USM(usm_file, key1, key2)
    output_path = Path(output) / f"{stem}"
    output_path.mkdir(exist_ok=True)
    file_paths = usm.demux(output_path=output_path)

    hca_files = file_paths.get("hca", [])
    try:
        flac_files = process_audio(hca_files, key1, key2, output_path)
    except RuntimeError:
        raise typer.Exit(1) from None
    file_paths.setdefault("flac", []).extend(flac_files)

    ass_files = process_subtitles(stem, output_path)
    file_paths.setdefault("ass", []).extend(ass_files)

    filtered_mkv: Path | None = None
    if vapoursynth:
        filtered_mkv = vapoursynth_filter(
            file_stem=stem,
            output_path=output_path,
            x265_params=x265_params,
        )
        if filtered_mkv:
            file_paths.setdefault("vs", []).append(filtered_mkv)
        else:
            log.warning(f"Failed to apply VapourSynth filter for {stem}, skipping...")

    mux(output_path, vs_path=filtered_mkv, fonts=fonts)

    if not no_cleanup:
        cleanup_files(file_paths, output_path)


@app.command()
def demux(
    usm_path: Annotated[str, typer.Argument(help="USM file or directory containing USM files.")],
    output: Annotated[str, typer.Option("--output", "-o", help="Output directory.")] = "output",
    no_cleanup: Annotated[
        bool,
        typer.Option(
            "--no-cleanup",
            "-nc",
            help="Do not delete decoded .ivf, .hca, and subtitle files when done.",
        ),
    ] = False,
    vapoursynth: Annotated[
        bool,
        typer.Option(
            "--vapoursynth",
            "-vs",
            help=(
                "Use VapourSynth for video processing. "
                "Looks for matching .py scripts in vs/ directory."
            ),
        ),
    ] = False,
    x265_params: Annotated[
        str,
        typer.Option(
            "--x265-params",
            help="Custom x265 parameters (colon-separated).",
        ),
    ] = "",
) -> None:
    usm_files = collect_files(Path(usm_path))
    log.info(f"Found {len(usm_files)} USM file(s).")
    Path(output).mkdir(exist_ok=True)
    fonts = fetch_font()

    for usm_file in usm_files:
        process_usm(usm_file, output, no_cleanup, vapoursynth, x265_params, fonts)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app()
