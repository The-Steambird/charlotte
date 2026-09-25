from itertools import pairwise
from pathlib import Path

import pytest

from conftest import flag_value, input_files
from stages.mux import mux, mux_args
from utils.errors import CharlotteError


TRACKS = {"fonts": [], "default_audio": "ja", "default_subtitle": "EN"}


def make_tracks(tmp_path, stem="Cs_Test", channels=("0", "1", "2"), subs=("EN", "JP")):
    video = tmp_path / f"{stem}.ivf"
    video.write_bytes(b"")
    audio = [tmp_path / f"{stem}_{channel}.flac" for channel in channels]
    subtitles = [tmp_path / "subs" / f"{stem}_{lang}.ass" for lang in subs]
    return video, audio, subtitles


def test_default_audio_sorted_first_and_flagged(ffmpeg, tmp_path):
    video, audio, subs = make_tracks(tmp_path)  # channels: 0=zh, 1=en, 2=ja
    mux(video, tmp_path / "o.mkv.part", audio, subs, **TRACKS)

    inputs = input_files(ffmpeg.cmd)
    assert inputs[0].endswith("Cs_Test.ivf")
    assert inputs[1].endswith("Cs_Test_2.flac")
    assert [Path(path).name for path in inputs[2:4]] == ["Cs_Test_0.flac", "Cs_Test_1.flac"]
    assert flag_value(ffmpeg.cmd, "-metadata:s:a:0") == "language=ja"
    assert flag_value(ffmpeg.cmd, "-disposition:a:0") == "default"
    assert flag_value(ffmpeg.cmd, "-disposition:a:1") == "0"
    assert flag_value(ffmpeg.cmd, "-disposition:a:2") == "0"


def test_default_subtitle_sorted_first_and_flagged(ffmpeg, tmp_path):
    video, audio, subs = make_tracks(tmp_path)
    mux(video, tmp_path / "o.mkv.part", audio, subs, **TRACKS | {"default_subtitle": "JP"})

    subtitle_inputs = [path for path in input_files(ffmpeg.cmd) if path.endswith(".ass")]
    assert subtitle_inputs[0].endswith("Cs_Test_JP.ass")
    assert flag_value(ffmpeg.cmd, "-metadata:s:s:0") == "language=ja"
    assert flag_value(ffmpeg.cmd, "-metadata:s:s:1") == "language=en"
    assert flag_value(ffmpeg.cmd, "-disposition:s:0") == "default"
    assert flag_value(ffmpeg.cmd, "-disposition:s:1") == "0"


def test_encode_tail_puts_codec_args_between_maps_and_metadata(tmp_path):
    """Video comes in on ffmpeg's stdin ahead of these args, which is why the tail has to
    start with the other inputs, place the codec options after the last -i (before it they
    would be read as input options) and end with the output. The .part name cannot tell
    ffmpeg which muxer to use."""
    _, audio, subs = make_tracks(tmp_path)
    output = tmp_path / "Cs_Test.mkv.part"
    args = mux_args(output, ["-c:v", "libx265", "-c:a", "copy"], audio, subs, **TRACKS)

    assert args[0] == "-i"
    assert args.index("-c:v") > max(i for i, flag in enumerate(args) if flag == "-i")
    assert args.index("-c:v") > args.index("-map")
    assert args.index("-c:v") < args.index("-metadata:s:a:0")
    assert args[-3:] == ["-f", "matroska", str(output)]


def test_fonts_only_ride_with_subtitle_tracks(tmp_path):
    """The two game fonts are 11 MB each and exist only for the .ass tracks, which is why a
    hard-subbed output (or one whose cutscene has no subtitles) must not carry them."""
    fonts = [tmp_path / "ja-jp.ttf", tmp_path / "zh-cn.ttf"]
    _, audio, subs = make_tracks(tmp_path)
    tracks = TRACKS | {"fonts": fonts}
    assert "-attach" in mux_args(tmp_path / "o.mkv", [], audio, subs, **tracks)
    assert "-attach" not in mux_args(tmp_path / "o.mkv", [], audio, [], **tracks)


def test_all_streams_mapped(ffmpeg, tmp_path):
    video, audio, subs = make_tracks(tmp_path)  # 1 video + 3 audio + 2 subtitles
    mux(video, tmp_path / "o.mkv.part", audio, subs, **TRACKS)
    maps = [value for flag, value in pairwise(ffmpeg.cmd) if flag == "-map"]
    assert maps == ["0", "1", "2", "3", "4", "5"]


def test_nostdin_ahead_of_the_first_input(ffmpeg, tmp_path):
    """mux passes nothing on stdin, and without the flag ffmpeg would inherit the GUI's
    command pipe under --json and read a byte off it per keyboard poll."""
    video, audio, subs = make_tracks(tmp_path)
    mux(video, tmp_path / "o.mkv.part", audio, subs, **TRACKS)
    assert ffmpeg.cmd.index("-nostdin") < ffmpeg.cmd.index("-i")


def test_missing_video_input_raises(ffmpeg, tmp_path):
    video, audio, subs = make_tracks(tmp_path)
    video.unlink()
    with pytest.raises(CharlotteError, match="input not found"):
        mux(video, tmp_path / "o.mkv.part", audio, subs, **TRACKS)


def test_no_audio_raises(ffmpeg, tmp_path):
    video, _, subs = make_tracks(tmp_path)
    with pytest.raises(CharlotteError, match="No audio files"):
        mux(video, tmp_path / "o.mkv.part", [], subs, **TRACKS)
