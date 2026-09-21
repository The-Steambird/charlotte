import numpy as np
import pytest

import stages.filter

from conftest import flag_value
from stages.ass import ASS
from stages.filter import (
    DEFAULT_CRF,
    DEFAULT_PRESET,
    X265_COLOUR_TAGS,
    burn_subtitle,
    encode_args,
    find_vs_script,
)


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


# --- burn_subtitle ---


def test_subtitle_rendered_at_frame_size_only_during_its_cue(tmp_path):
    """The converted .ass declares PlayRes 384x288 and libass has to scale that to the clip.
    On a 1080p frame the line then lands in the bottom band at a legible height, where an
    unscaled render would be about 7 px tall near row 271. Nothing is drawn once the cue
    ends."""
    import vapoursynth as vs

    srt = tmp_path / "Cs_A_EN.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    ass = ASS(srt, "EN")
    ass.parse_srt()
    subtitle = ass.convert_to_ass(tmp_path)
    clip = vs.core.std.BlankClip(width=1920, height=1080, format=vs.YUV420P8, length=90)

    burnt = burn_subtitle(clip, subtitle, fonts=[])

    luma = np.asarray(burnt.get_frame(30)[0])
    lit_rows = np.flatnonzero(luma.max(axis=1) > 128)
    assert lit_rows.min() > 900
    assert lit_rows.max() - lit_rows.min() > 20
    assert np.asarray(burnt.get_frame(89)[0]).max() == 0
