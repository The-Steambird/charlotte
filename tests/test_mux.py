from itertools import pairwise
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from conftest import flag_value, input_files
from stages.mux import mux, mux_args, subset_fonts


TRACKS = {"fonts": [], "default_audio": "ja", "default_subtitle": "EN"}


def make_tracks(tmp_path):
    video = tmp_path / "Cs_Test.ivf"
    video.write_bytes(b"")
    audio = [tmp_path / f"Cs_Test_{channel}.flac" for channel in ("0", "1", "2")]
    subtitles = [tmp_path / "subs" / f"Cs_Test_{lang}.ass" for lang in ("EN", "JP")]
    return video, audio, subtitles


def build_font(path: Path, text: str) -> None:
    names = {ord(char): f"uni{ord(char):04X}" for char in text}
    order = [".notdef", *names.values()]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(names)
    builder.setupGlyf({name: TTGlyphPen(None).glyph() for name in order})
    builder.setupHorizontalMetrics(dict.fromkeys(order, (500, 0)))
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Test", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.save(path)


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
    """The fonts exist only for the .ass tracks, which is why a hard-subbed output (or one
    whose cutscene has no subtitles) must not carry them."""
    fonts = [tmp_path / "ja-jp.ttf", tmp_path / "zh-cn.ttf"]
    _, audio, subs = make_tracks(tmp_path)
    tracks = TRACKS | {"fonts": fonts}
    assert "-attach" in mux_args(tmp_path / "o.mkv", [], audio, subs, **tracks)
    assert "-attach" not in mux_args(tmp_path / "o.mkv", [], audio, [], **tracks)


def test_font_subset_keeps_what_harfbuzz_splits_thai_into(tmp_path):
    """The fonts have no shaping tables for Thai, which is why HarfBuzz splits ำ into ํ and า
    itself and the subset has to keep both."""
    font = tmp_path / "zh-cn.ttf"
    build_font(font, "Aำํา")
    subtitle = tmp_path / "Cs_Test_TH.ass"
    subtitle.write_text("ำ", encoding="utf-8")
    folder = tmp_path / "subs"
    folder.mkdir()

    [subset] = subset_fonts([font], [subtitle], folder)

    with TTFont(subset) as ttf:
        assert set(ttf.getBestCmap()) == {ord("ำ"), ord("ํ"), ord("า")}


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
