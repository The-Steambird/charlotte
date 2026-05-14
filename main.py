from pathlib import Path
from typing import Annotated

import typer

from decoders.ass import ASS
from decoders.hca import HCA
from decoders.usm import USM
from utils.filter import vapoursynth_filter
from utils.keys import get_decryption_key
from utils.languages import SUBTITLES_LANGUAGES
from utils.logger import log
from utils.mux import mux


app = typer.Typer(help="USM video file demuxer and converter")


def collect_files(input_path: Path, extension: str) -> list[Path]:
    if input_path.is_file():
        return [input_path]

    if input_path.is_dir():
        files = list(input_path.glob(f"*.{extension}"))
        if not files:
            log.error(f"No .{extension} files found in directory")
            raise typer.Exit(1)
        return files

    log.error(f"Error: {input_path} is not a valid file or directory")
    raise typer.Exit(1)


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
            help="Use VapourSynth for video processing. Looks for matching .py scripts in vs/ directory.",
        ),
    ] = False,
    x265_params: Annotated[
        str,
        typer.Option(
            "--x265-params",
            help="Custom x265 parameters (colon-separated). Default: ",
        ),
    ] = "",
) -> None:
    """
    Demux USM file(s) to extract video/audio tracks and mux them into MKV container.
    """
    usm_files = collect_files(Path(usm_path), "usm")
    log.info(f"Found {len(usm_files)} USM file(s).")
    Path(output).mkdir(exist_ok=True)

    for usm_file in usm_files:
        basename_fixes = {
            "Cs_4131904_HaiDaoChuXian_Boy": "Cs_Activity_4001103_Summertime_Boy",
            "Cs_4131904_HaiDaoChuXian_Girl": "Cs_Activity_4001103_Summertime_Girl",
            "Cs_200211_WanYeXianVideo": "Cs_DQAQ200211_WanYeXianVideo",
        }

        stem = usm_file.stem
        if stem in basename_fixes:
            stem = basename_fixes.get(stem, stem)

        log.info(f"Processing: {usm_file.name}")
        keys = get_decryption_key(usm_file.name)
        if keys is None:
            log.warning(f"Could not find decryption keys for {usm_file.name}, skipping...")
            continue
        
        key1, key2 = keys
        usm = USM(usm_file, key1, key2)
        output_path = Path(output) / f"{stem}"
        output_path.mkdir(exist_ok=True)
        file_paths = usm.demux(output_path=output_path)

        for hca_file in file_paths["hca"]:
            hca = HCA(hca_file, key1, key2)
            hca.decrypt()
            flac_file = hca.convert_to_flac(output_path=output_path)
            file_paths.setdefault("flac", []).append(flac_file)

        subtitle_files = []
        input_path = Path.cwd() / "Subtitle"
        for lang in SUBTITLES_LANGUAGES:
            lang_path = input_path / f"{lang}"
            subtitle_path = lang_path / f"{stem}_{lang}.srt"
            if subtitle_path.exists():
                subtitle_files.append(subtitle_path)

        log.info(f"Found {len(subtitle_files)} subtitle file(s).")

        for sub_file in subtitle_files:
            lang = sub_file.stem.split("_")[-1]
            try:
                ass = ASS(str(sub_file), lang)
                if ass.parse_srt():
                    ass_path = ass.convert_to_ass(output_path=output_path)
                    file_paths.setdefault("ass", []).append(ass_path)
            except Exception as e:
                log.error(f"Error processing subtitle: {e}")

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

        mux(output_path, vs_path=filtered_mkv)

        if not no_cleanup:
            try:
                for value in file_paths.values():
                    for file in value:
                        file.unlink()

                if output_path.joinpath("subs").is_dir():
                    output_path.joinpath("subs").rmdir()
            except PermissionError:
                log.error(f"Failed to clean up files and directories for {stem}")


if __name__ == "__main__":
    app()
