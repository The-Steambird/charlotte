import subprocess

from pathlib import Path

import numpy as np
import pytest

import stages.filter

from conftest import flag_value
from stages.ass import ASS
from stages.filter import (
    DEFAULT_CRF,
    DEFAULT_PRESET,
    X265_COLOUR_TAGS,
    encode_args,
    filter_escape,
    find_vs_script,
    subtitle_filter,
)
from utils.ffmpeg import ffmpeg_path


# --- find_vs_script ---


@pytest.fixture
def vs_dir(tmp_path, monkeypatch):
    """Point the bundled vs/ script directory at a scratch dir."""
    monkeypatch.setattr(stages.filter, "bundle_root", lambda: tmp_path)
    scripts = tmp_path / "vs"
    scripts.mkdir()
    return scripts


def test_find_vs_script_gender_fallback(vs_dir):
    (vs_dir / "Cs_A_Boy.py").touch()
    (vs_dir / "Cs_B_Girl.py").touch()
    assert find_vs_script("Cs_A_Girl") == "Cs_A_Boy"
    assert find_vs_script("Cs_B_Boy") == "Cs_B_Girl"


def test_find_vs_script_prefers_exact_over_counterpart(vs_dir):
    (vs_dir / "Cs_A_Boy.py").touch()
    (vs_dir / "Cs_A_Girl.py").touch()
    assert find_vs_script("Cs_A_Girl") == "Cs_A_Girl"


def test_find_vs_script_missing(vs_dir):
    assert find_vs_script("Cs_A_Boy") is None
    assert find_vs_script("Cs_NoGender") is None


def test_find_vs_script_default_is_last_resort(vs_dir):
    (vs_dir / "default.py").touch()
    (vs_dir / "Cs_A_Boy.py").touch()
    assert find_vs_script("Cs_A_Boy") == "Cs_A_Boy"
    assert find_vs_script("Cs_A_Girl") == "Cs_A_Boy"
    assert find_vs_script("Cs_NoGender") == "default"


# --- encode_args ---


def test_crf_and_preset_keep_builtin_params():
    cmd = encode_args(20.0, "medium")
    assert "psy-rd=2.0" in flag_value(cmd, "-x265-params")
    assert flag_value(cmd, "-crf") == "20.0"
    assert flag_value(cmd, "-preset") == "medium"


def test_explicit_params_replace_builtin_ahead_of_colour_tags():
    cmd = encode_args(DEFAULT_CRF, DEFAULT_PRESET, "rd=6")
    assert flag_value(cmd, "-x265-params") == f"rd=6:{X265_COLOUR_TAGS}"


def test_colour_tags_go_through_x265_only():
    """-colorspace on an untagged Y4M input makes ffmpeg convert, not tag."""
    cmd = encode_args(DEFAULT_CRF, DEFAULT_PRESET)
    assert flag_value(cmd, "-x265-params").endswith(X265_COLOUR_TAGS)
    assert not {"-colorspace", "-color_primaries", "-color_trc"} & set(cmd)


def test_other_tracks_copied_and_muxer_named():
    """The encode writes the final MKV itself, which is why audio and subtitles ride along
    as copies. The .part output name cannot tell ffmpeg which muxer to use."""
    cmd = encode_args(DEFAULT_CRF, DEFAULT_PRESET)
    assert flag_value(cmd, "-c:v") == "libx265"
    assert flag_value(cmd, "-c:a") == "copy"
    assert flag_value(cmd, "-c:s") == "copy"
    assert flag_value(cmd, "-f") == "matroska"
    assert "-vf" not in cmd


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
    """The converted .ass declares PlayRes 384x288 and libass has to scale that to the frame.
    On a 1080p frame the line then lands in the bottom band at a legible height, where an
    unscaled render would be about 7 px tall near row 271. Nothing is drawn once the cue
    ends. Four black frames at 1 fps go through the bundled ffmpeg and come back raw. The
    subtitle sits under a directory named with every character `filter_escape` has to
    protect, because only ffmpeg's own parser can prove the escaping right."""
    if not ffmpeg_path().exists():
        pytest.skip("bundled ffmpeg.exe is not present")
    filters = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-filters"], capture_output=True, text=True
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
    )  # fmt: skip
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    planes = np.fromfile(raw, dtype=np.uint8).reshape(frames, height * 3 // 2, width)
    luma = planes[1, :height]
    lit_rows = np.flatnonzero(luma.max(axis=1) > 128)
    assert lit_rows.min() > 900
    assert lit_rows.max() - lit_rows.min() > 20
    assert planes[3, :height].max() == 0
