import re

from pathlib import Path

from utils.logger import log


class ASS:
    def __init__(self, srt_file: str, lang: str | None = None, custom_style: str | None = None):
        self.srt_file = Path(srt_file)
        self.lang = lang
        self.custom_style = custom_style
        self.fontname = "SDK_JP_Web" if lang == "JP" else "SDK_SC_Web"
        self.dialog_lines: list[str] = []

    def parse_srt(self) -> bool:
        with open(self.srt_file, encoding="utf-8") as f:
            content = f.read()

        lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

        i = 0
        while i < len(lines):
            if not lines[i].strip():
                i += 1
                continue

            if not lines[i].strip().isdigit():
                i += 1
                continue

            if i + 2 >= len(lines):
                break

            timing_line = lines[i + 1]
            timing_match = re.findall(r"-?\d\d:\d\d:\d\d,\d\d", timing_line)

            if len(timing_match) != 2:
                i += 3
                continue

            formatted_times = []
            for time_str in timing_match:
                formatted_time = time_str.replace("-0", "0").replace(",", ".")
                if formatted_time.startswith("0"):
                    formatted_time = formatted_time[1:]
                formatted_times.append(formatted_time)

            shadow = r"{\xshad-0.05\yshad-0.05\blur0.5}"
            dialog = f"Dialogue: 0,{formatted_times[0]},{formatted_times[1]},Default,,0,0,0,,{shadow}{lines[i + 2]}"
            i += 2

            if (i + 1 < len(lines)) and lines[i + 1].strip():
                if not lines[i + 1].strip().isdigit():
                    i += 1
                    dialog = f"{dialog}\\n{lines[i]}"

            self.dialog_lines.append(dialog)
            i += 1

        if not self.dialog_lines:
            if self.srt_file.stat().st_size == 0:
                log.info(f"{self.srt_file.name} is empty or doesn't exist, skipping...")
            else:
                log.warning(f"{self.srt_file} is empty or has incorrect format.")
            return False

        return True

    def convert_to_ass(self, output_path: Path) -> Path:
        output_path = output_path / "subs"
        output_path.mkdir(parents=True, exist_ok=True)

        output_file = output_path / (self.srt_file.stem + ".ass")

        ass_content = [
            "[Script Info]",
            "; This is an Advanced Sub Station Alpha v4+ script.",
            "ScriptType: v4.00+",
            "Collisions: Normal",
            "ScaledBorderAndShadow: yes",
            "PlayDepth: 0",
            "PlayResX: 384",
            "PlayResY: 288",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
        ]

        if self.custom_style:
            style_line = self.custom_style.replace("{fontname}", self.fontname)
            ass_content.append(style_line)
        else:
            # Default style matching official style.
            style_params = [
                "Style: Default",  # Format: Name
                f"{self.fontname}",  # Fontname
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
            style = ",".join(style_params)
            ass_content.append(style)

        ass_content.extend(
            [
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Actor, MarginL, MarginR, MarginV, Effect, Text",
            ]
        )

        for line in self.dialog_lines:
            if line.strip():
                line = re.sub(r"<([ubi])>", r"{\\$11}", line)
                line = re.sub(r"</([ubi])>", r"{\\$10}", line)
                line = re.sub(
                    r'<font\s+color="?#(\w{2})(\w{2})(\w{2})"?>',
                    r"{\\c&H$3$2$1&}",
                    line,
                )
                line = re.sub(r"</font>", "", line)
                ass_content.append(line)

        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(ass_content))

        return output_file
