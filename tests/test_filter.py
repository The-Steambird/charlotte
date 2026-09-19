import pytest

import stages.filter

from conftest import flag_value
from stages.filter import (
    DEFAULT_CRF,
    DEFAULT_PRESET,
    X265_COLOUR_TAGS,
    ffmpeg_params,
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


# --- ffmpeg_params ---


def test_crf_and_preset_keep_builtin_params(tmp_path):
    cmd = ffmpeg_params(tmp_path / "out.mkv", 20.0, "medium")
    assert "psy-rd=2.0" in flag_value(cmd, "-x265-params")
    assert flag_value(cmd, "-crf") == "20.0"
    assert flag_value(cmd, "-preset") == "medium"
    assert cmd[-1] == str(tmp_path / "out.mkv")


def test_explicit_params_replace_builtin_ahead_of_colour_tags(tmp_path):
    cmd = ffmpeg_params(tmp_path / "out.mkv", DEFAULT_CRF, DEFAULT_PRESET, "rd=6")
    assert flag_value(cmd, "-x265-params") == f"rd=6:{X265_COLOUR_TAGS}"


def test_colour_tags_go_through_x265_only(tmp_path):
    """-colorspace on an untagged Y4M input makes ffmpeg convert, not tag."""
    cmd = ffmpeg_params(tmp_path / "out.mkv", DEFAULT_CRF, DEFAULT_PRESET)
    assert flag_value(cmd, "-x265-params").endswith(X265_COLOUR_TAGS)
    assert not {"-colorspace", "-color_primaries", "-color_trc"} & set(cmd)
