import io
import subprocess

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import stages.encode

from conftest import flag_value
from stages.ass import ASS
from stages.encode import (
    DEFAULT_CRF,
    DEFAULT_PRESET,
    X265_COLOUR_TAGS,
    encode_args,
    filter_escape,
    find_vs_script,
    parse_ffmpeg_stderr,
    subtitle_filter,
)
from utils.ffmpeg import ffmpeg_path


# --- find_vs_script ---


@pytest.fixture
def vs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stages.encode, "bundle_root", lambda: tmp_path)
    scripts = tmp_path / "vs"
    scripts.mkdir()
    return scripts


@pytest.mark.parametrize(
    "scripts, stem, expected",
    [
        (["Cs_A_Boy"], "Cs_A_Girl", "Cs_A_Boy"),
        (["Cs_A_Girl"], "Cs_A_Boy", "Cs_A_Girl"),
        (["Cs_A_Boy", "Cs_A_Girl"], "Cs_A_Girl", "Cs_A_Girl"),
        (["Cs_A_Boy", "default"], "Cs_A_Girl", "Cs_A_Boy"),
        (["Cs_A_Boy", "default"], "Cs_NoGender", "default"),
        ([], "Cs_A_Boy", None),
    ],
    ids=[
        "girl-to-boy",
        "boy-to-girl",
        "exact-over-counterpart",
        "counterpart-over-default",
        "default-last",
        "missing",
    ],
)
def test_find_vs_script(vs_dir, scripts, stem, expected):
    for name in scripts:
        (vs_dir / f"{name}.py").touch()
    assert find_vs_script(stem) == expected


# --- encode_args ---


@pytest.mark.parametrize(
    "preset, slow", [("ultrafast", False), ("medium", False), ("slow", True), ("placebo", True)]
)
def test_effort_knobs_ride_only_with_the_slow_presets(preset, slow):
    effort = {"bframes=8", "rd=4", "ref=6"}
    params = set(flag_value(encode_args(DEFAULT_CRF, preset), "-x265-params").split(":"))
    assert "psy-rd=2.0" in params
    assert params & effort == (effort if slow else set())


@pytest.mark.parametrize(
    "given, expected",
    [("rd=6", f"rd=6:{X265_COLOUR_TAGS}"), ("", X265_COLOUR_TAGS)],
    ids=["replaces-the-tuning", "empty-means-the-bare-preset"],
)
def test_given_params_replace_the_tuning_ahead_of_colour_tags(given, expected):
    cmd = encode_args(DEFAULT_CRF, DEFAULT_PRESET, given)
    assert flag_value(cmd, "-x265-params") == expected


def test_colour_tags_go_through_x265_only():
    """-colorspace on an untagged Y4M input makes ffmpeg convert, not tag."""
    cmd = encode_args(DEFAULT_CRF, DEFAULT_PRESET)
    assert flag_value(cmd, "-x265-params").endswith(X265_COLOUR_TAGS)
    assert not {"-colorspace", "-color_primaries", "-color_trc"} & set(cmd)


# --- ffmpeg output ---


def test_frame_count_moves_the_bar_and_other_lines_keep_their_level(reporter):
    stderr = (
        b"frame=12\n"
        b"fps=30.0\n"
        b"progress=continue\n"
        b"\n"
        b"x265 [warning]: frame size limited\n"
        b"x265 [error]: out of memory\n"
        b"[matroska @ 000001] Starting new cluster\n"
    )
    process = SimpleNamespace(stderr=io.BytesIO(stderr))

    with reporter.task("ffmpeg", 100) as task:
        parse_ffmpeg_stderr(process, task, reporter)

    assert reporter.progress == [("ffmpeg", 12)]
    assert reporter.logs == [
        ("warning", "x265 [warning]: frame size limited"),
        ("error", "x265 [error]: out of memory"),
        ("info", "[matroska @ 000001] Starting new cluster"),
    ]


# --- hard-sub ---


def test_subtitle_filter_names_the_font_directory_only_when_fonts_exist():
    font = Path("C:/font/ja-jp.ttf")
    graph = subtitle_filter(Path("C:/subs/Cs_A_EN.ass"), [font])
    assert graph.startswith("ass=filename=")
    assert graph.endswith(f":fontsdir={filter_escape(str(font.parent))}")
    assert "fontsdir" not in subtitle_filter(Path("C:/subs/Cs_A_EN.ass"), [])


def test_filter_escape_doubles_backslashes_twice():
    assert filter_escape(r"C:\out\Cs_A'1\subs.ass") == r"C\\:\\\\out\\\\Cs_A\\\'1\\\\subs.ass"
    assert filter_escape("a,b;c[d]") == r"a\,b\;c\[d\]"


def test_subtitle_rendered_at_frame_size_only_during_its_cue(tmp_path):
    """libass has to scale the .ass PlayRes 384x288 to the frame, and an unscaled render on
    1080p would be about 7 px tall near row 271. The subtitle sits under a directory named
    with every character `filter_escape` has to protect, because only ffmpeg's own parser
    can prove the escaping right."""
    if not ffmpeg_path().exists():
        pytest.skip("bundled ffmpeg.exe is not present")
    filters = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-filters"], capture_output=True, text=True, check=True
    ).stdout
    if " ass " not in filters:
        pytest.skip("bundled ffmpeg was built without libass")

    sub_dir = tmp_path / "a' , [b];c"
    sub_dir.mkdir()
    srt = sub_dir / "Cs_A_EN.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    ass = ASS(srt, "EN")
    ass.parse_srt()
    subtitle = ass.convert_to_ass(sub_dir)

    width, height, frames = 1920, 1080, 4
    frame = bytes(width * height * 3 // 2)
    y4m = b"YUV4MPEG2 W1920 H1080 F1:1 Ip A1:1 C420mpeg2\n" + b"FRAME\n".join(
        [b""] + [frame] * frames
    )
    raw = tmp_path / "out.yuv"
    result = subprocess.run(
        [
            str(ffmpeg_path()),
            "-hide_banner", "-v", "error",
            "-f", "yuv4mpegpipe", "-i", "pipe:0",
            "-vf", subtitle_filter(subtitle, []),
            "-pix_fmt", "yuv420p", "-f", "rawvideo", str(raw),
        ],
        input=y4m,
        capture_output=True,
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    planes = np.fromfile(raw, dtype=np.uint8).reshape(frames, height * 3 // 2, width)
    luma = planes[1, :height]
    lit_rows = np.flatnonzero(luma.max(axis=1) > 128)
    assert lit_rows.min() > 900
    assert lit_rows.max() - lit_rows.min() > 20
    assert planes[3, :height].max() == 0
