import re

from typing import TYPE_CHECKING

from utils.logger import log


if TYPE_CHECKING:
    from pathlib import Path


class ASS:
    timing = re.compile(r"-?\d\d:\d\d:\d\d,\d\d")
    shadow = r"{\xshad-0.05\yshad-0.05\blur0.5}"
    html_tag_fix = (
        # <b> → {\b1},  <i> → {\i1},  <u> → {\u1}
        (re.compile(r"<([ubi])>"), r"{\\\g<1>1}"),
        # </b> → {\b0},  </i> → {\i0},  </u> → {\u0}
        (re.compile(r"</([ubi])>"), r"{\\\g<1>0}"),
        # <font color="#RRGGBB"> → {\c&HBBGGRR&} (ASS uses BGR order)
        (re.compile(r'<font\s+color="?#(\w{2})(\w{2})(\w{2})"?>'), r"{\\c&H\3\2\1&}"),
        # </font> → (removed)
        (re.compile(r"</font>"), ""),
    )

    def __init__(self, srt_file: Path, lang: str):
        self.srt_file = srt_file
        self.font = "SDK_JP_Web" if lang == "JP" else "SDK_SC_Web"
        self.dialog_lines: list[str] = []

    def parse_srt(self) -> bool:
        # Some SRT files carry a BOM which breaks the digit check on the first block.
        content = self.srt_file.read_text(encoding="utf-8-sig")
        blocks = re.split(r"\n{2,}", "\n".join(content.splitlines()).strip())

        for block in blocks:
            lines = block.split("\n")
            if len(lines) < 3 or not lines[0].strip().isdigit():
                continue

            timings = self.timing.findall(lines[1])
            if len(timings) != 2:
                continue

            # HH:MM:SS,cc -> H:MM:SS.cc
            start, end = (t.lstrip("-").replace(",", ".").removeprefix("0") for t in timings)

            # Renderers only honor soft breaks (\n) under WrapStyle 2 and draw them as spaces, so
            # we use hard line break instead.
            text = "\\N".join(line for line in lines[2:] if line.strip())

            self.dialog_lines.append(
                f"Dialogue: 0,{start},{end},Default,,0,0,0,,{self.shadow}{text}"
            )

        if not self.dialog_lines:
            if self.srt_file.stat().st_size != 0:
                log.warning(f"{self.srt_file} is empty or has incorrect format.")
            return False

        return True

    def convert_to_ass(self, output_path: Path) -> Path:
        output_path = output_path / "subs"
        output_path.mkdir(parents=True, exist_ok=True)
        output_file = output_path / (self.srt_file.stem + ".ass")

        # Default style matching official GI subtitle style.
        style_line = ",".join(
            [
                "Style: Default",  # Name
                self.font,  # Font
                "10.9",  # Fontsize
                "&H00FFFFFF",  # PrimaryColour
                "&H000000FF",  # SecondaryColour
                "&H00484848",  # OutlineColour
                "&H00484848",  # BackColour
                "0",  # Bold
                "0",  # Italic
                "0",  # Underline
                "0",  # StrikeOut
                "100.0",  # ScaleX
                "100.0",  # ScaleY
                "0.0",  # Spacing
                "0.0",  # Angle
                "1",  # BorderStyle
                "0.05",  # Outline
                "0.05",  # Shadow
                "2",  # Alignment
                "10",  # MarginL
                "10",  # MarginR
                "17",  # MarginV
                "1",  # Encoding
            ]
        )

        header = (
            "[Script Info]\n"
            "; This is an Advanced Sub Station Alpha v4+ script.\n"
            "ScriptType: v4.00+\n"
            "Collisions: Normal\n"
            "ScaledBorderAndShadow: yes\n"
            "PlayDepth: 0\n"
            "PlayResX: 384\n"
            "PlayResY: 288\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"{style_line}\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Actor, MarginL, MarginR, MarginV, Effect, Text"
        )

        events = []
        for line in self.dialog_lines:
            tagged = line
            for pattern, repl in self.html_tag_fix:
                tagged = pattern.sub(repl, tagged)
            events.append(tagged)

        output_file.write_text("\n".join([header, *events]), encoding="utf-8")
        return output_file
