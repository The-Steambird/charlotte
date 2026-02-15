"""Charlotte - USM video file demuxer and converter.

A tool for extracting and converting video/audio from CRI Middleware USM files.
"""

from pathlib import Path
from typing import Annotated

import typer

from decoders.ass import ASS
from decoders.hca import HCA
from decoders.usm import USM
from utils.keys import get_decryption_key
from utils.languages import SUBTITLES_LANGUAGES
from utils.mux import mux


app = typer.Typer(help="USM video file demuxer and converter")


def collect_files(input_path: str | Path, extension: str) -> list[Path]:
    if isinstance(input_path, str):
        input_path = Path(input_path)
    if input_path.is_file():
        return [input_path]

    if input_path.is_dir():
        files = list(input_path.glob(f"*.{extension}"))
        if not files:
            typer.echo(f"No .{extension} files found in directory", err=True)
            raise typer.Exit(1)
        return files

    typer.echo(f"Error: {input_path} is not a valid file or directory", err=True)
    raise typer.Exit(1)


@app.command()
def demux(
    usm_path: Annotated[
        str, typer.Argument(help="USM file or directory containing USM files.")
    ],
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output directory.")
    ] = "output",
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
            "--vapoursynth", "-vs", help="Use Vapoursynth for video processing."
        ),
    ] = False,
) -> None:
    """
    Demux USM file(s) to extract video/audio tracks and mux them into MKV container.
    """
    usm_files = collect_files(usm_path, "usm")
    typer.echo(f"Found {len(usm_files)} USM file(s).")
    Path(output).mkdir(exist_ok=True)

    for usm_file in usm_files:
        typer.echo(f"\nProcessing: {usm_file.name}")
        key1, key2 = get_decryption_key(usm_file.name)
        usm = USM(usm_file, key1, key2)
        output_path = Path(output).joinpath(usm_file.stem)
        output_path.mkdir(exist_ok=True)
        file_paths = usm.demux(output_path=output_path)

        for hca_file in file_paths["hca"]:
            hca = HCA(hca_file, key1, key2)
            hca.decrypt()
            flac_file = hca.convert_to_flac(output_path=output_path)
            file_paths.setdefault("flac", []).append(flac_file)

        basename_fixes = {
            "Cs_4131904_HaiDaoChuXian_Boy": "Cs_Activity_4001103_Summertime_Boy",
            "Cs_4131904_HaiDaoChuXian_Girl": "Cs_Activity_4001103_Summertime_Girl",
            "Cs_200211_WanYeXianVideo": "Cs_DQAQ200211_WanYeXianVideo",
        }

        usm_filename = usm_file.stem
        if usm_filename in basename_fixes:
            usm_filename = basename_fixes.get(usm_filename)

        subtitle_files = []
        input_path = Path.cwd().joinpath("Subtitle")
        for lang in SUBTITLES_LANGUAGES:
            lang_path = input_path.joinpath(lang)
            subtitle_path = lang_path.joinpath(f"{usm_filename}_{lang}.srt")
            if subtitle_path.exists():
                subtitle_files.append(subtitle_path)

        typer.echo(f"Found {len(subtitle_files)} subtitle file(s).")

        for sub_file in subtitle_files:
            lang = sub_file.stem.split("_")[-1]
            try:
                ass = ASS(str(sub_file), lang)
                if ass.parse_srt():
                    ass_path = ass.convert_to_ass(output_path=output_path)
                    file_paths.setdefault("ass", []).append(ass_path)
            except Exception as e:
                typer.echo(f"Error: {e}", err=True)

        mux(output_path)

        if not no_cleanup:
            for value in file_paths.values():
                for file in value:
                    file.unlink()

            output_path.joinpath("subs")
            if output_path.joinpath("subs").is_dir():
                output_path.joinpath("subs").rmdir()


if __name__ == "__main__":
    app()
