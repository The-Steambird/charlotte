from stages.ass import ASS


# Mirrors ASS.shadow to test failure if shadow was changed.
SHADOW = r"{\xshad-0.05\yshad-0.05\blur0.5}"

# Fake SRT content.
BASIC_SRT = """1
00:00:01,234 --> 00:00:02,345
Hello
"""


def write_srt(tmp_path, text, name="Cs_Test_EN.srt"):
    srt = tmp_path / name
    srt.write_text(text, encoding="utf-8")
    return srt


def convert_ass(tmp_path, text, lang=None, custom_style=None):
    ass = ASS(str(write_srt(tmp_path, text)), lang=lang, custom_style=custom_style)
    assert ass.parse_srt()
    return ass.convert_to_ass(tmp_path / "out").read_text(encoding="utf-8")


# --- SRT parsing ---


def test_timing_truncated_to_centiseconds(tmp_path):
    """HH:MM:SS,cc → H:MM:SS.cc"""
    ass = ASS(str(write_srt(tmp_path, BASIC_SRT)))
    assert ass.parse_srt()
    assert ass.dialog_lines == [f"Dialogue: 0,0:00:01.23,0:00:02.34,Default,,0,0,0,,{SHADOW}Hello"]


def test_negative_timing_sign_stripped(tmp_path):
    """Strip negative timing sign (no idea why some files have this)."""
    srt = "1\n-00:00:00,500 --> 00:00:01,500\nHello\n"
    ass = ASS(str(write_srt(tmp_path, srt)))
    assert ass.parse_srt()
    assert ass.dialog_lines[0].startswith("Dialogue: 0,0:00:00.50,0:00:01.50,")


def test_two_line_dialogue_joined(tmp_path):
    """Test dialogues with multiple lines."""
    srt = "1\n00:00:01,000 --> 00:00:02,000\nOne\nTwo\n"
    ass = ASS(str(write_srt(tmp_path, srt)))
    assert ass.parse_srt()
    assert ass.dialog_lines[0].endswith(r"One\nTwo")


def test_malformed_blocks_skipped(tmp_path):
    srt = (
        "abc\n00:00:01,000 --> 00:00:02,000\nNo index\n"
        "\n"
        "2\nnot a timing line\nNo timing\n"
        "\n"
        "3\n00:00:03,000 --> 00:00:04,000\nValid\n"
    )
    ass = ASS(str(write_srt(tmp_path, srt)))
    assert ass.parse_srt()
    assert len(ass.dialog_lines) == 1
    assert ass.dialog_lines[0].endswith("Valid")


def test_empty_srt_returns_false(tmp_path):
    ass = ASS(str(write_srt(tmp_path, "")))
    assert not ass.parse_srt()


def test_unparseable_srt_returns_false(tmp_path):
    ass = ASS(str(write_srt(tmp_path, "garbage\nwith no valid blocks\n")))
    assert not ass.parse_srt()


# --- fonts and styles ---


def test_font_by_language():
    assert ASS("x.srt", lang="JP").font == "SDK_JP_Web"
    assert ASS("x.srt", lang="EN").font == "SDK_SC_Web"
    assert ASS("x.srt").font == "SDK_SC_Web"


def test_default_style_and_header(tmp_path):
    content = convert_ass(tmp_path, BASIC_SRT)
    assert "ScriptType: v4.00+" in content
    assert "PlayResX: 384" in content
    assert "Style: Default,SDK_SC_Web,10.9," in content
    assert "[Events]" in content


def test_jp_style_uses_jp_font(tmp_path):
    content = convert_ass(tmp_path, BASIC_SRT, lang="JP")
    assert "Style: Default,SDK_JP_Web,10.9," in content


def test_custom_style_fontname_placeholder(tmp_path):
    content = convert_ass(
        tmp_path, BASIC_SRT, lang="JP", custom_style="Style: Custom,{fontname},20"
    )
    assert "Style: Custom,SDK_JP_Web,20" in content


# --- ASS conversion ---


def test_output_path_and_name(tmp_path):
    ass = ASS(str(write_srt(tmp_path, BASIC_SRT)))
    assert ass.parse_srt()
    output = ass.convert_to_ass(tmp_path / "out")
    assert output == tmp_path / "out" / "subs" / "Cs_Test_EN.ass"
    assert output.is_file()


def test_html_tags_converted_to_ass_overrides(tmp_path):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n"
        '<b>bold</b> <i>it</i> <u>ul</u> <font color="#A1B2C3">red</font>\n'
        "\n"
        "2\n00:00:03,000 --> 00:00:04,000\n"
        "<font color=#0D0E0F>unquoted</font>\n"
    )
    content = convert_ass(tmp_path, srt)
    assert r"{\b1}bold{\b0}" in content
    assert r"{\i1}it{\i0}" in content
    assert r"{\u1}ul{\u0}" in content
    # ASS colors are BGR, #A1B2C3 becomes &HC3B2A1&.
    assert r"{\c&HC3B2A1&}red" in content
    assert r"{\c&H0F0E0D&}unquoted" in content
    # Check if any raw HTML survived in the dialogue lines (everything after [Events]).
    assert "<" not in content.split("[Events]")[1]
