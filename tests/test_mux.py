import subprocess
import types

from itertools import pairwise
from pathlib import Path

import pytest

import stages.mux

from stages.mux import mux
from utils.errors import CharlotteError


def make_output(tmp_path, stem="Cs_Test", channels=("0", "1", "2"), subs=("EN", "JP")):
    """Lay out a demuxed cutscene directory: video, one audio file per channel, subs/."""
    output = tmp_path / stem
    (output / "subs").mkdir(parents=True)
    (output / f"{stem}.ivf").write_bytes(b"")
    for channel in channels:
        (output / f"{stem}_{channel}.flac").write_bytes(b"")
    for lang in subs:
        (output / "subs" / f"{stem}_{lang}.ass").write_bytes(b"")
    return output


# --- helpers to read the flat ffmpeg argument list ---


def input_files(cmd):
    """Every file passed to ffmpeg via -i, in order: video first, then audio, then subtitles."""
    return [value for flag, value in pairwise(cmd) if flag == "-i"]


def flag_value(cmd, flag):
    """The argument following a flag, e.g. flag_value(cmd, "-disposition:a:0") == "default"."""
    return cmd[cmd.index(flag) + 1]


@pytest.fixture
def ffmpeg(monkeypatch):
    """Capture the ffmpeg command instead of running it and set return code to simulate failure."""
    capture = types.SimpleNamespace(cmd=None, returncode=0)

    def fake_run(cmd, **kwargs):
        capture.cmd = cmd
        return subprocess.CompletedProcess(cmd, capture.returncode, stdout="", stderr="")

    monkeypatch.setattr(stages.mux.subprocess, "run", fake_run)
    return capture


def test_default_audio_sorted_first_and_flagged(ffmpeg, tmp_path):
    output = make_output(tmp_path)  # channels: 0=zh, 1=en, 2=ja
    mux(output, default_audio="ja")

    inputs = input_files(ffmpeg.cmd)
    assert inputs[0].endswith("Cs_Test.ivf")
    assert inputs[1].endswith("Cs_Test_2.flac")  # ja leads
    assert {Path(path).name for path in inputs[2:4]} == {"Cs_Test_0.flac", "Cs_Test_1.flac"}
    assert flag_value(ffmpeg.cmd, "-metadata:s:a:0") == "language=ja"
    assert flag_value(ffmpeg.cmd, "-disposition:a:0") == "default"
    assert flag_value(ffmpeg.cmd, "-disposition:a:1") == "0"
    assert flag_value(ffmpeg.cmd, "-disposition:a:2") == "0"


def test_default_subtitle_sorted_first_and_flagged(ffmpeg, tmp_path):
    output = make_output(tmp_path)
    mux(output, default_subtitle="JP")

    subtitle_inputs = [path for path in input_files(ffmpeg.cmd) if path.endswith(".ass")]
    assert subtitle_inputs[0].endswith("Cs_Test_JP.ass")
    assert flag_value(ffmpeg.cmd, "-metadata:s:s:0") == "language=ja"
    assert flag_value(ffmpeg.cmd, "-metadata:s:s:1") == "language=en"
    assert flag_value(ffmpeg.cmd, "-disposition:s:0") == "default"
    assert flag_value(ffmpeg.cmd, "-disposition:s:1") == "0"


def test_vs_output_replaces_video_input(ffmpeg, tmp_path):
    output = make_output(tmp_path)
    vs_path = output / "Cs_Test_filtered.mkv"
    vs_path.write_bytes(b"")
    mux(output, vs_path=vs_path)
    assert input_files(ffmpeg.cmd)[0].endswith("Cs_Test_filtered.mkv")


def test_audio_glob_follows_extension(ffmpeg, tmp_path):
    output = make_output(tmp_path, channels=())
    (output / "Cs_Test_2.mka").write_bytes(b"")
    mux(output, audio_extension=".mka")
    # Input 0 is the video; the .mka must be picked up as the first audio input.
    assert input_files(ffmpeg.cmd)[1].endswith("Cs_Test_2.mka")


def test_all_streams_mapped(ffmpeg, tmp_path):
    output = make_output(tmp_path)  # 1 video + 3 audio + 2 subtitles
    mux(output)
    maps = [value for flag, value in pairwise(ffmpeg.cmd) if flag == "-map"]
    assert maps == ["0", "1", "2", "3", "4", "5"]


def test_fonts_attached_when_given(ffmpeg, tmp_path):
    output = make_output(tmp_path)
    mux(output, fonts=(tmp_path / "ja-jp.ttf", tmp_path / "zh-cn.ttf"))
    assert ffmpeg.cmd.count("-attach") == 2
    assert flag_value(ffmpeg.cmd, "-metadata:s:t:0") == "mimetype=application/x-truetype-font"


def test_no_fonts_no_attachments(ffmpeg, tmp_path):
    output = make_output(tmp_path)
    mux(output)
    assert "-attach" not in ffmpeg.cmd


def test_output_mkv_is_last_argument(ffmpeg, tmp_path):
    output = make_output(tmp_path)
    mux(output)
    assert ffmpeg.cmd[-1] == str(output / "Cs_Test.mkv")


def test_missing_video_input_raises(ffmpeg, tmp_path):
    output = make_output(tmp_path)
    (output / "Cs_Test.ivf").unlink()
    with pytest.raises(CharlotteError, match="input not found"):
        mux(output)


def test_no_audio_raises(ffmpeg, tmp_path):
    output = make_output(tmp_path, channels=())
    with pytest.raises(CharlotteError, match="No audio files"):
        mux(output)


def test_ffmpeg_failure_raises(ffmpeg, tmp_path):
    output = make_output(tmp_path)
    ffmpeg.returncode = 1
    with pytest.raises(CharlotteError, match="exited with code 1"):
        mux(output)
