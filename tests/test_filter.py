from pathlib import Path

import pytest

import stages.filter

from stages.filter import DEFAULT_CRF, DEFAULT_PRESET, ffmpeg_params, find_vs_script


# --- find_vs_script ---


@pytest.fixture
def vs_dir(tmp_path, monkeypatch):
    """Point the bundled vs/ script directory at a scratch dir."""
    monkeypatch.setattr(stages.filter, "bundle_root", lambda: tmp_path)
    scripts = tmp_path / "vs"
    scripts.mkdir()
    return scripts


def test_find_vs_script_exact_match(vs_dir):
    (vs_dir / "Cs_A_Boy.py").touch()
    assert find_vs_script("Cs_A_Boy") == "Cs_A_Boy"


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


# --- ffmpeg_params ---


def flag_value(cmd, flag):
    """The argument following a flag in the ffmpeg argument list,
    e.g. flag_value(cmd, "-preset") == "slower"."""
    return cmd[cmd.index(flag) + 1]


def test_defaults_inject_builtin_x265_params(tmp_path):
    cmd = ffmpeg_params(Path("ffmpeg.exe"), tmp_path / "out.mkv", DEFAULT_CRF, DEFAULT_PRESET)
    assert "aq-mode=3" in flag_value(cmd, "-x265-params")
    assert flag_value(cmd, "-crf") == str(DEFAULT_CRF)
    assert flag_value(cmd, "-preset") == DEFAULT_PRESET
    assert cmd[-1] == str(tmp_path / "out.mkv")


def test_custom_crf_suppresses_builtin_params(tmp_path):
    cmd = ffmpeg_params(Path("ffmpeg.exe"), tmp_path / "out.mkv", 14.0, DEFAULT_PRESET)
    assert "-x265-params" not in cmd


def test_custom_preset_suppresses_builtin_params(tmp_path):
    cmd = ffmpeg_params(Path("ffmpeg.exe"), tmp_path / "out.mkv", DEFAULT_CRF, "medium")
    assert "-x265-params" not in cmd


def test_explicit_params_used_verbatim(tmp_path):
    cmd = ffmpeg_params(
        Path("ffmpeg.exe"), tmp_path / "out.mkv", DEFAULT_CRF, DEFAULT_PRESET, "rd=6"
    )
    assert flag_value(cmd, "-x265-params") == "rd=6"
