<p style="text-align: center;">
  <img width="2100" height="auto" src="https://raw.githubusercontent.com/lunarmint/charlotte/master/docs/imgs/banner.png" alt="Charlotte banner" />
</p>

<p style="text-align: center;"><i>Hi there! I'm Charlotte, a journalist with The Steambird~</i></p>
<p style="text-align: center;"><sub>Art credit: <a href="https://www.pixiv.net/en/artworks/117728570">Kuromitsuri Tomato</a></sub></p>

---
<p style="text-align: center;">
  <a href="https://github.com/lunarmint/charlotte/releases/latest"><img src="https://img.shields.io/github/v/release/lunarmint/charlotte?label=release" alt="Release" /></a>
  <a href="https://github.com/lunarmint/charlotte/releases"><img src="https://img.shields.io/github/downloads/lunarmint/charlotte/total" alt="Downloads" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.14%2B-3776AB?logo=python&logoColor=white" alt="Python 3.14+" /></a>
  <a href="https://docs.astral.sh/ruff/"><img src="https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=111111" alt="Lint: Ruff" /></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/badge/package%20manager-uv-4B5DFF" alt="Package Manager: uv" /></a>
  <a href="https://github.com/lunarmint/charlotte/blob/master/LICENSE"><img src="https://img.shields.io/github/license/lunarmint/charlotte" alt="License" /></a>
  <a href="https://github.com/lunarmint/charlotte/stargazers"><img src="https://img.shields.io/github/stars/lunarmint/charlotte?style=social" alt="GitHub stars" /></a>
</p>

# Charlotte

Charlotte is a Genshin Impact utility that losslessly decrypts `.usm` cutscene files into playable `.mkv` videos.

- Supports EN, CN, JP, KR audio tracks
- Supports subtitles in 15 languages
- Optional VapourSynth pipeline for post-processing quality improvements

All known cutscenes from versions 1.0 through 6.5 can be decrypted.

If you have missing keys, pull requests are welcome. I fetch keys myself, but some old keys may be missing.

## Why Charlotte

Who else would archive Teyvat's cutscenes but its best journalist?

This project is heavily inspired by [GI-cutscenes](https://github.com/ToaHartor/GI-cutscenes). Charlotte rebuilds the workflow at a higher level and aims to add extras over time, including VapourSynth processing and a GUI.

## Features

- [x] Decrypt `.usm` into `.ivf` video and `.hca` audio
- [x] Convert `.srt` subtitles into styled `.ass` with matching official cutscene subtitle style and fonts
- [x] Convert `.hca` audio to `.flac` for archival
- [x] Automatically fetches subtitles from DimBreath and fonts from the game directory
- [x] Mux tracks into `.mkv`
- [x] Full VapourSynth processing workflow
- [ ] Graphical User Interface

VapourSynth filter scripts take a lot of time to write to ensure quality, hence they will be slowly added over time. If you have encoding knowledge, contributions are welcome!

I should also mention that the VapourSynth filters are extremely heavy on CPU and GPU (to a lesser degree), so it's recommended to have a powerful machine for optimal performance.

## Quick Start (Windows Binary)

### Prerequisites

1. Download `charlotte.exe` from the [latest release](https://github.com/lunarmint/charlotte/releases/latest).
2. Put [ffmpeg.exe](https://www.gyan.dev/ffmpeg/builds/#release-builds) and [mkvmerge.exe](https://mkvtoolnix.download/downloads.html#windows) in the same directory as `charlotte.exe`.
3. Locate `.usm` files at:
```
[Game Directory]\Genshin Impact game\GenshinImpact_Data\StreamingAssets\VideoAssets\StandaloneWindows64
```

Note: the availability of older cutscenes depends on your local game files and resource cleanup history.

### Usage

```sh
charlotte [PATH_TO_USM_FILE_OR_DIR] [OPTIONS]
```

Example:

```sh
charlotte "USM\Cs_EQHDJ005_HaiDengJie_Girl.usm" -vs -nc

```

For help:

```sh
charlotte --help
```

This decrypts the cutscene, applies the VapourSynth filter script, and writes to `output/Cs_EQHDJ005_HaiDengJie_Boy/Cs_EQHDJ005_HaiDengJie_Boy.mkv` without deleting intermediate files.

**Tip**: If you're running with `-vs` flag, for higher encoding speed, setting Python and FFmpeg in Task Manager to high priority can help. Alternatively, you can leave the terminal on the front so that Windows' Process Scheduling Priority will prioritize Charlotte.

### Parameters

| Type | Flag | Alias | Description |
| --- | --- |-----| --- |
| Argument | `PATH_TO_USM_FILE_OR_DIR` | `-` | Path to one `.usm` file or a directory containing `.usm` files. |
| Option | `--output [DIR]` | `-o` | Output directory (default: `output`). |
| Option | `--no-cleanup` | `-nc` | Keep intermediate files (`.ivf`, `.hca`, `.ass`, etc.). |
| Option | `--vapoursynth` | `-vs` | Apply a matching VapourSynth `.vpy` filter script. |
| Option | `--crf [VALUE]` | `-crf` | x265 CRF value for VapourSynth output (default: `13.5`). Setting this suppresses the built-in `--x265-params` defaults. |
| Option | `--preset [PRESET]` | `-preset` | x265 preset for VapourSynth output (default: `slower`). Setting this suppresses the built-in `--x265-params` defaults. |
| Option | `--x265-params [PARAMS]` | `-x265` | Custom x265 params (colon-separated). Overrides the built-in defaults below. |

When neither `--crf`, `--preset`, nor `--x265-params` is set, the following x265 params are applied automatically:

```
keyint=300:min-keyint=30:no-open-gop=1:ref=6:bframes=8:lookahead-slices=0:rc-lookahead=60:aq-mode=3:aq-strength=0.75:qcomp=0.72:cbqpoffs=-2:crqpoffs=-2:no-cutree=1:rd=4:psy-rd=2.0:psy-rdoq=1.7:max-merge=5:no-strong-intra-smoothing=1:tskip=1:deblock=-2,-2:no-sao=1:no-sao-non-deblock=1
```

Setting `--crf` or `--preset` suppresses these params, letting x265 use its own defaults for everything else. To combine custom crf/preset with custom x265 params, use `--x265-params` explicitly (it always takes full precedence).

## Build From Source

### Prerequisites

- Python 3.14 or higher
- [uv](https://github.com/astral-sh/uv)

Install dependencies:
```sh
uv sync
```

Run the project:
```
uv run main.py USM/Cs_EQHDJ005_HaiDengJie_Boy.usm -vs -nc
```

For flag options, refer to the [Parameters](#parameters) section.

### Build Command

```sh
uv run pyinstaller charlotte.spec
```

## ❤️ Support

If you enjoyed using Charlotte, your support would mean so much to me. It keeps me motivated to invest more time into the project and keep it alive for as long as I can.

**[GitHub Sponsors](https://github.com/sponsors/lunarmint)**
