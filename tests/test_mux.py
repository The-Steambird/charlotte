from itertools import pairwise
from pathlib import Path

import pytest

from conftest import flag_value
from stages.mux import mux, mux_args
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


def input_files(cmd):
    """Every -i argument in order: video, then audio, then subtitles."""
    return [value for flag, value in pairwise(cmd) if flag == "-i"]


def test_default_audio_sorted_first_and_flagged(ffmpeg, tmp_path):
    output = make_output(tmp_path)  # channels: 0=zh, 1=en, 2=ja
    mux(output, default_audio="ja")

    inputs = input_files(ffmpeg.cmd)
    assert inputs[0].endswith("Cs_Test.ivf")
    assert inputs[1].endswith("Cs_Test_2.flac")
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


def test_encode_tail_puts_codec_args_between_maps_and_metadata(tmp_path):
    """Video comes in on ffmpeg's stdin ahead of these args, which is why the tail has to
    start with the other inputs, place the codec options after the last -i (before it they
    would be read as input options) and end with the output."""
    output = make_output(tmp_path)
    args = mux_args(output, output / "Cs_Test.mkv.part", ["-c:v", "libx265", "-c:a", "copy"])

    assert args[0] == "-i"
    assert args.index("-c:v") > max(i for i, flag in enumerate(args) if flag == "-i")
    assert args.index("-c:v") > args.index("-map")
    assert args.index("-c:v") < args.index("-metadata:s:a:0")
    assert args[-1] == str(output / "Cs_Test.mkv.part")


def test_hard_sub_output_carries_no_soft_subtitles(tmp_path):
    output = make_output(tmp_path, subs=("EN", "JP", "DE"))
    args = mux_args(output, output / "out.mkv", ["-c", "copy"], subtitles=False)

    assert not [path for path in input_files(args) if path.endswith(".ass")]
    assert "-disposition:s:0" not in args
    assert len(input_files(args)) == 3  # the audio tracks still ride along


def test_fonts_only_ride_with_subtitle_tracks(tmp_path):
    """The two game fonts are 11 MB each and exist only for the .ass tracks, which is why a
    hard-subbed output (or one whose cutscene has no subtitles) must not carry them."""
    fonts = [tmp_path / "ja-jp.ttf", tmp_path / "zh-cn.ttf"]
    output = make_output(tmp_path)
    assert "-attach" not in mux_args(output, output / "o.mkv", [], fonts=fonts, subtitles=False)

    output = make_output(tmp_path, stem="Cs_NoSubs", subs=())
    assert "-attach" not in mux_args(output, output / "o.mkv", [], fonts=fonts)


def test_audio_glob_follows_extension(ffmpeg, tmp_path):
    output = make_output(tmp_path, channels=())
    (output / "Cs_Test_2.mka").write_bytes(b"")
    mux(output, audio_extension=".mka")
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


def test_command_frame(ffmpeg, tmp_path):
    """-nostdin ahead of the first input: mux passes nothing on stdin, so ffmpeg would
    otherwise inherit the GUI's command pipe under --json and read a byte off it per
    keyboard poll. Output last, no attachments without fonts."""
    output = make_output(tmp_path)
    mux(output)
    assert ffmpeg.cmd.index("-nostdin") < ffmpeg.cmd.index("-i")
    assert ffmpeg.cmd[-1] == str(output / "Cs_Test.mkv")
    assert "-attach" not in ffmpeg.cmd


def test_missing_video_input_raises(ffmpeg, tmp_path):
    output = make_output(tmp_path)
    (output / "Cs_Test.ivf").unlink()
    with pytest.raises(CharlotteError, match="input not found"):
        mux(output)


def test_no_audio_raises(ffmpeg, tmp_path):
    output = make_output(tmp_path, channels=())
    with pytest.raises(CharlotteError, match="No audio files"):
        mux(output)
