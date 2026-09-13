<p align="center">
  <img width="2100" height="auto" src="https://raw.githubusercontent.com/The-Steambird/charlotte/master/docs/imgs/banner.png" alt="Charlotte banner" />
</p>

<p style="text-align: center;"><i>Hi there! I'm Charlotte, a journalist with The Steambird~</i></p>
<p style="text-align: center;"><sub>Art credit: <a href="https://www.pixiv.net/en/artworks/117728570">Kuromitsuri Tomato</a></sub></p>

---
<p style="text-align: center;">
  <a href="https://github.com/The-Steambird/charlotte/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/The-Steambird/charlotte/ci.yml?branch=master&label=ci&logo=githubactions&logoColor=white" alt="CI" /></a>
  <a href="https://github.com/The-Steambird/charlotte/releases/latest"><img src="https://img.shields.io/github/v/release/The-Steambird/charlotte?label=release" alt="Release" /></a>
  <a href="https://github.com/The-Steambird/charlotte/releases"><img src="https://img.shields.io/github/downloads/The-Steambird/charlotte/total" alt="Downloads" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.14%2B-3776AB?logo=python&logoColor=white" alt="Python 3.14+" /></a>
  <a href="https://docs.astral.sh/ruff/"><img src="https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=111111" alt="Lint: Ruff" /></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/badge/package%20manager-uv-4B5DFF" alt="Package Manager: uv" /></a>
  <a href="https://github.com/The-Steambird/charlotte/blob/master/LICENSE"><img src="https://img.shields.io/github/license/The-Steambird/charlotte" alt="License" /></a>
  <a href="https://github.com/The-Steambird/charlotte/stargazers"><img src="https://img.shields.io/github/stars/The-Steambird/charlotte?style=social" alt="GitHub stars" /></a>
</p>

# Charlotte

Charlotte is a Genshin Impact utility that losslessly decrypts `.usm` cutscene files into playable
`.mkv` videos, covering all known cutscenes from versions 1.0 through 7.0. Charlotte is also able to
retrieve keys directly from USM file itself, although an explicitly defined key is still preferred
for processing speed.

This project is heavily inspired by [GI-cutscenes](https://github.com/ToaHartor/GI-cutscenes).
Charlotte not only rebuilds the workflow at a higher level, it also has various optimizations to the
decryption algorithm to be significantly more efficient (and even faster than the original
implementation despite being on Python). Charlotte also add extras with tons of QoLs (see below),
VapourSynth processing, and a GUI. Also credits
to [UsmDiviner](https://github.com/Senkin219/UsmDiviner) for inspiring me with the key guessing
algorithm.

If you have missing keys, pull requests are welcome.

Disclaimer: This tool is purely for educational purpose and aims to archive already released game
content.

## Features

- Losslessly decrypt `.usm` into `.mkv` video with EN, CN, JP, KR audio tracks
- Significantly improved decryption algorithm compared to GI-cutscenes implementation
- Key recovery algorithm for USM files without a key
- Softsub in 15 languages styled to match official cutscene subtitle style and font
- Automatically syncs subtitle cache (15 languages) from DimBreath
- Automatically fetches new video keys from upstream and get fonts from the game directory
- VapourSynth pipeline for post-processing quality improvements
- Bundled lightweight custom FFmpeg build at only ~15MB
- Built-in self updater
- Graphical User Interface (coming soon)

VapourSynth filter scripts take a lot of time to write to ensure quality, hence they will be slowly
added over time. If you have encoding knowledge, contributions are welcome!

I should also mention that the VapourSynth filters are extremely heavy on CPU and GPU (to a lesser
degree), so it's recommended to have a powerful machine for optimal performance.

## Quick Start (Windows Binary)

### Prerequisites

1. Download `charlotte.exe` from
   the [latest release](https://github.com/The-Steambird/charlotte/releases/latest).
2. Locate `.usm` files at:

```
[Game Directory]\Genshin Impact game\GenshinImpact_Data\StreamingAssets\VideoAssets\StandaloneWindows64
```

Note: the availability of older cutscenes depends on your local game files and resource cleanup
history.

### Usage

```sh
charlotte [PATHS...] [OPTIONS]
```

`PATHS` is one or more `.usm` files and/or directories containing `.usm` files.

Example:

```sh
charlotte "USM\Cs_Cutscene_Something_Girl.usm" -vs -nc
```

This decrypts the cutscene, applies the VapourSynth filter script, and writes to
`output/Cs_EQHDJ005_HaiDengJie_Girl/Cs_EQHDJ005_HaiDengJie_Girl.mkv` without deleting intermediate
files.

Process several files and/or directories at once:

```sh
charlotte "USM\Cs_A.usm" "USM\Cs_B.usm" "USM\Cs_More_Cutscenes.usm" -o output
```

To check what is available for your files (decryption key, local subtitles, VapourSynth script)
without processing anything:

```sh
charlotte "USM\Cs_Cutscene_Something_Girl.usm" --probe
```

To recover key straight from the USM file and report them without demuxing or converting:

```sh
charlotte "USM\Cs_Cutscene_Something_Girl.usm" --crack
```

To check for a newer release, and install it in place after confirmation:

```sh
charlotte --update
```

For help:

```sh
charlotte --help
```

**Tip**: If you're running with `-vs` flag, for higher encoding speed, setting Python and FFmpeg in
Task Manager to high priority can help. Alternatively, you can leave the terminal on the front so
that Windows' Process Scheduling Priority will prioritize Charlotte.

### Parameters

| Type     | Flag                     | Alias     | Description                                                                                                                                    |
|----------|--------------------------|-----------|------------------------------------------------------------------------------------------------------------------------------------------------|
| Argument | `PATHS...`               | `-`       | One or more `.usm` files and/or directories containing `.usm` files.                                                                           |
| Option   | `--output [DIR]`         | `-o`      | Output directory (default: `output`).                                                                                                          |
| Option   | `--flat`                 | `-f`      | Write `{name}.mkv` directly into the output directory instead of a per-cutscene subfolder.                                                     |
| Option   | `--skip-existing`        | `-se`     | Skip any file whose output `.mkv` already exists.                                                                                              |
| Option   | `--no-cleanup`           | `-nc`     | Keep intermediate files (`.ivf`, `.hca`, `.ass`, etc.).                                                                                        |
| Option   | `--audio-codec [CODEC]`  | `-ac`     | Audio codec for muxed tracks: `flac` (default, lossless) or `opus` for smaller size.                                                           |
| Option   | `--default-audio [LANG]` | `-da`     | Select default audio language: `zh`, `en`, `ja` (default), `ko`.                                                                               |
| Option   | `--default-sub [CODE]`   | `-ds`     | Select default subtitle: `chs`, `cht`, `de`, `en` (default), `es`, `fr`, `id`, `it`, `jp`, `kr`, `pt`, `ru`, `th`, `tr`, `vi`.                 |
| Option   | `--key [KEY]`            | `-k`      | Manually input a key for a single file                                                                                                         |
| Option   | `--vapoursynth`          | `-vs`     | Apply a matching VapourSynth filter script from `vs/`.                                                                                         |
| Option   | `--crf [VALUE]`          | `-crf`    | x265 CRF value for VapourSynth output (default: `13.5`).                                                                                       |
| Option   | `--preset [PRESET]`      | `-preset` | x265 preset for VapourSynth output (default: `slower`).                                                                                        |
| Option   | `--x265-params [PARAMS]` | `-x265`   | Custom x265 params (colon-separated). Overrides the built-in defaults below.                                                                   |
| Option   | `--probe`                | `-p`      | Only report what is available for each file (decryption key, local subtitles, VapourSynth script). Read-only: nothing is processed or fetched. |
| Option   | `--crack`                | `-c`      | Recover key from USM file and report it, without demuxing or converting.                                                                       |
| Option   | `--json`                 | `-json`   | Emit newline-delimited JSON events on stdout for a GUI/automation frontend.                                                                    |
| Option   | `--update`               | `-u`      | Check GitHub for a newer release and update.                                                                                                   |
| Option   | `--version`              | `-v`      | Print the Charlotte version and exit.                                                                                                          |

When `-vs` option is used, if neither `--crf`, `--preset`, nor `--x265-params` is set, the following
x265 params are applied
automatically:

```
keyint=300:min-keyint=30:no-open-gop=1:ref=6:bframes=8:lookahead-slices=0:rc-lookahead=60:aq-mode=3:aq-strength=0.75:qcomp=0.72:cbqpoffs=-2:crqpoffs=-2:no-cutree=1:rd=4:psy-rd=2.0:psy-rdoq=1.7:max-merge=5:no-strong-intra-smoothing=1:tskip=1:deblock=-2,-2:no-sao=1:no-sao-non-deblock=1
```

These options are highly optimized for video quality, I do not recommend changing it unless you have
strong video encoding knowledge.

Setting `--crf` or `--preset` suppresses these params, letting x265 use its own defaults for
everything else. To combine custom crf/preset with custom x265 params, use `--x265-params`
explicitly (it always takes full precedence).

## Build From Source

### Prerequisites

- Python 3.14 or higher
- [uv](https://github.com/astral-sh/uv)
- FFmpeg (see below)

Install dependencies:

```sh
uv sync
```

Run the project:

```
uv run main.py USM/Cs_EQHDJ005_HaiDengJie_Boy.usm -vs -nc
```

For flag options, refer to the [Parameters](#parameters) section.

### Custom FFmpeg Build

The bundled `ffmpeg.exe` is a lightweight custom build. To rebuild it:

1. Set up [media-autobuild_suite](https://github.com/m-ab-s/media-autobuild_suite).
2. Copy `ffmpeg_options.txt` from the repo root to `<suite>/build/ffmpeg_options.txt`.
3. To force a rebuild after changing options, delete `<suite>/local64/bin-video/ffmpeg.exe` before
   running `media-autobuild_suite.bat`.
4. Copy the resulting `<suite>/local64/bin-video/ffmpeg.exe` to the repo root.

Setting this up takes time (a few hours), especially on the very first run. If you wish to avoid
that, you can get my prebuilt
from [here](https://github.com/The-Steambird/charlotte/releases/tag/tools).

### Build Command

```sh
uv run pyinstaller charlotte.spec
```

## ❤️ Support

If you enjoyed using Charlotte, your support would mean so much to me. It keeps me motivated to
invest more time into the project and keep it alive for as long as I can.

**[GitHub Sponsors](https://github.com/sponsors/lunarmint)**
