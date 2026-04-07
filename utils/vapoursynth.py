"""VapourSynth processing and encoding utilities."""
import subprocess
from pathlib import Path

import typer
import vapoursynth as vs


def vs_filter(
    vpy_script: Path,
    output_mkv: Path,
    x265_params: str = "",
) -> bool:
    ffmpeg_path = Path.cwd() / "ffmpeg.exe"

    if not ffmpeg_path.exists():
        typer.echo("Error: ffmpeg.exe not found in root directory.", err=True)
        return False

    if not x265_params:
        default_x265_params = [
            "profile=main10",
            "cutree=0",
            "deblock=-2,-2",
            "no-sao=1",
            "tskip=1",
            "cbqpoffs=-3",
            "qcomp=0.7",
            "lookahead-slices=0",
            "keyint=300",
            "min-keyint=30",
            "max-merge=5",
            "ref=6",
            "bframes=12",
            "rd=4",
            "psy-rd=2.0",
            "psy-rdoq=1.5",
            "aq-mode=3",
            "aq-strength=0.7",
            "transfer=1",
            "colorprim=1",
            "colormatrix=1",
            "colorrange=1"
        ]
        x265_params = ":".join(default_x265_params)

    typer.echo(f"Applying VapourSynth filters: {vpy_script.name}")

    # Load and evaluate the VapourSynth script - this will give proper Python tracebacks
    with open(vpy_script, encoding="utf-8") as f:
        script_content = f.read()

    # Execute the script in its own namespace
    script_globals = {"__name__": "__vapoursynth__", "__file__": str(vpy_script.absolute())}
    exec(script_content, script_globals)

    # Get the output clip (vs scripts use set_output, which registers clips)
    if not vs.get_outputs():
        typer.echo("Error: No output clips found in VapourSynth script", err=True)
        return False

    # Get output 0 (default output)
    clip = vs.get_outputs()[0]

    # Prepare ffmpeg command
    ffmpeg_cmd = [
        str(ffmpeg_path),
        "-y",
        "-f", "yuv4mpegpipe",
        "-i", "pipe:0",
        "-c:v", "libx265",
        "-pix_fmt", "yuv420p10le",
        "-preset", "slower",
        "-crf", "12",
        "-x265-params", x265_params,
        str(output_mkv),
    ]

    ffmpeg_process = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Write Y4M header
        y4m_header = f"YUV4MPEG2 W{clip.width} H{clip.height} F{clip.fps.numerator}:{clip.fps.denominator} Ip A0:0 C{_get_y4m_colorspace(clip)} XCOLORRANGE=LIMITED\n"
        ffmpeg_process.stdin.write(y4m_header.encode())

        # Write frames
        for i, frame in enumerate(clip.frames(close=True)):
            ffmpeg_process.stdin.write(b"FRAME\n")
            for plane_num in range(frame.format.num_planes):
                plane_data = frame[plane_num]
                ffmpeg_process.stdin.write(bytes(plane_data))

        ffmpeg_process.stdin.close()
        ffmpeg_stdout, ffmpeg_stderr = ffmpeg_process.communicate()

        if ffmpeg_process.returncode != 0:
            typer.echo(f"Error: ffmpeg failed with code {ffmpeg_process.returncode}", err=True)
            if ffmpeg_stderr:
                typer.echo(f"ffmpeg error: {ffmpeg_stderr.decode()}", err=True)
            return False

        typer.echo(f"Encoded: {output_mkv.name}")
        return True

    finally:
        vs.clear_outputs()


def _get_y4m_colorspace(clip: vs.VideoNode) -> str:
    """Get Y4M colorspace identifier from VapourSynth clip."""
    if clip.format.color_family == vs.YUV:
        if clip.format.bits_per_sample == 8:
            if clip.format.subsampling_w == 1 and clip.format.subsampling_h == 1:
                return "420"
            elif clip.format.subsampling_w == 1 and clip.format.subsampling_h == 0:
                return "422"
            elif clip.format.subsampling_w == 0 and clip.format.subsampling_h == 0:
                return "444"
        else:
            bits = clip.format.bits_per_sample
            if clip.format.subsampling_w == 1 and clip.format.subsampling_h == 1:
                return f"420p{bits}"
            elif clip.format.subsampling_w == 1 and clip.format.subsampling_h == 0:
                return f"422p{bits}"
            elif clip.format.subsampling_w == 0 and clip.format.subsampling_h == 0:
                return f"444p{bits}"
    return "420p10"
