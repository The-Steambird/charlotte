<p align="center">
  <img width="35%" height="auto" src="https://raw.githubusercontent.com/The-Steambird/charlotte/master/docs/imgs/logo.png" alt="Charlotte logo" />
</p>

<p align="center">
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

[<img src="https://raw.githubusercontent.com/The-Steambird/charlotte/master/docs/imgs/download.svg"/>](https://github.com/The-Steambird/charlotte/releases/download/1.2.0/charlotte-1.2.0.zip)
> Stable build version: `1.2.0` (Released on: October 6th, 2026).

Charlotte is a Genshin Impact utility that losslessly decrypts `.usm` cutscene files into playable
`.mkv` videos, covering all known cutscenes from versions 1.0 through 7.1. Each video contains all 4
language audio tracks, subtitles in 15 languages styled to match nearly perfectly with official
style. There's also an optional [VapourSynth](https://www.vapoursynth.com/about/) filter pass to
clean up banding and macroblocking introduced by Hoyo's incompetent encoding (mostly banding and
macro blocking). If you're a fan of high quality picture, rejoice.

<p align="center">
   <img width="49%" alt="main UI" src="https://github.com/user-attachments/assets/0dbb5334-d0e9-497b-9b40-c7276e4d86b3" />
   <img width="49%" alt="settings UI" src="https://github.com/user-attachments/assets/aa67118b-7ed5-49b2-8eff-4402208fc46b" />
</p>

## Features

- Graphical User Interface
- Losslessly decrypt `.usm` into `.mkv` video
- EN, CN, JP, KR audio tracks + softsub in 15 languages
- Near perfect official subtitle styling
- Key crack for USM files without a key (up to 7.0)
- Subtitles fetched from Dimbreath automatically
- Font subsetting using only necessary text characters (a few hundred KBs per file)
- Automatically fetches new video keys
- Automatically fetches fonts from the game directory
- VapourSynth pipeline for post-processing quality improvements
- Bundled lightweight custom FFmpeg build at only ~15MB
- Built-in self updater
- Multi-language support

## Getting started

1. Download and extract the `.zip`
2. Run `charlotte-gui.exe`

## Command line

### Usage

```sh
charlotte-cli [PATHS...] [OPTIONS]
```

`PATHS` is one or more `.usm` files and/or directories containing `.usm` files.

Example:

```sh
charlotte-cli "USM\7.1\Cs_ZD_AQ70200801_TFR_Boy.usm" -vs -nc
```

This decrypts the cutscene, applies the VapourSynth filter script, and writes to
`output/Cs_ZD_AQ70200801_TFR_Boy/Cs_ZD_AQ70200801_TFR_Boy.mkv` without deleting intermediate
files.

I should also mention that the VapourSynth filters are extremely heavy on CPU and GPU (to a lesser
degree), so it's recommended to have a powerful machine for optimal performance.

Process several files and/or directories at once:

```sh
charlotte-cli "USM\6.3\Cs_NodKrai_AQ60161901_BSHMO_Boy.usm" "USM\7.1\Cs_ZD_AQ70190901_HE.usm" "USM\7.1\Cs_ZD_AQ70200801_TFR_Boy.usm" -o batch
```

To check what is available for your files (decryption key, local subtitles, VapourSynth script)
without processing anything:

```sh
charlotte-cli "USM\7.1\Cs_ZD_AQ70190901_HE.usm" --probe
```

To recover keys straight from the USM file and report them without demuxing or converting:

```sh
charlotte-cli "USM\6.3\Cs_NodKrai_AQ60161901_BSHMO_Boy.usm" --crack
```

To check for a newer release, and install it in place after confirmation:

```sh
charlotte-cli --update
```

For help:

```sh
charlotte-cli --help
```

**Tip**: If you're running with `-vs` flag, for higher encoding speed, setting charlotte-cli.exe to
High priority in Task Manager > Details can help. Alternatively, you can leave the
terminal on the front so that Windows' Process Scheduling Priority will prioritize Charlotte.

### Options

| Type     | Flag                     | Alias     | Description                                                                                                                    |
|----------|--------------------------|-----------|--------------------------------------------------------------------------------------------------------------------------------|
| Argument | `PATHS...`               | `-`       | One or more `.usm` files and/or directories containing `.usm` files.                                                           |
| Option   | `--output [DIR]`         | `-o`      | Output directory (default: `output`).                                                                                          |
| Option   | `--flat`                 | `-f`      | Write `{name}.mkv` directly into the output directory instead of a per-cutscene subfolder.                                     |
| Option   | `--skip-existing`        | `-se`     | Skip any file whose output `.mkv` already exists.                                                                              |
| Option   | `--no-cleanup`           | `-nc`     | Keep intermediate files (`.ivf`, `.hca`, `.ass`, etc.).                                                                        |
| Option   | `--audio-codec [CODEC]`  | `-ac`     | Audio codec for muxed tracks: `flac` (default, lossless) or `opus` for smaller size.                                           |
| Option   | `--default-audio [LANG]` | `-da`     | Select default audio language: `zh`, `en`, `ja` (default), `ko`.                                                               |
| Option   | `--default-sub [CODE]`   | `-ds`     | Select default subtitle: `chs`, `cht`, `de`, `en` (default), `es`, `fr`, `id`, `it`, `jp`, `kr`, `pt`, `ru`, `th`, `tr`, `vi`. |
| Option   | `--key [KEY]`            | `-k`      | Decryption key for a single file. Use `audioKey:aesKey` format for a 7.1+ file.                                                |
| Option   | `--vapoursynth`          | `-vs`     | Apply a matching VapourSynth filter script from `vs/`.                                                                         |
| Option   | `--hard-sub`             | `-hs`     | Burn the default subtitle language into the video with x265 and skip softsub muxing.                                           |
| Option   | `--crf [VALUE]`          | `-crf`    | x265 CRF value for re-encoded output, i.e. `-vs` or `-hs` (default: `13.5`).                                                   |
| Option   | `--preset [PRESET]`      | `-preset` | x265 preset for re-encoded output, i.e. `-vs` or `-hs` (default: `slower`).                                                    |
| Option   | `--x265-params [PARAMS]` | `-x265`   | Custom x265 params (colon-separated). Overrides the built-in defaults below.                                                   |
| Option   | `--probe`                | `-p`      | Only report what is available for each file (decryption key, local subtitles, VapourSynth script).                             |
| Option   | `--crack`                | `-c`      | Recover key from USM file and report it, without demuxing or converting.                                                       |
| Option   | `--json`                 | `-json`   | Emit newline-delimited JSON events on stdout for a GUI/automation frontend.                                                    |
| Option   | `--update`               | `-u`      | Check GitHub for a newer release and update.                                                                                   |
| Option   | `--version`              | `-v`      | Print the Charlotte version and exit.                                                                                          |

### Encoding defaults

When `-vs` or `-hs` is used, the following x265 params are applied automatically unless
`--x265-params` is set:

```
keyint=300:min-keyint=30:no-open-gop=1:aq-mode=3:aq-strength=0.75:qcomp=0.72:cbqpoffs=-2:crqpoffs=-2:no-cutree=1:psy-rd=2.0:psy-rdoq=1.7:no-strong-intra-smoothing=1:deblock=-2,-2:no-sao=1:no-sao-non-deblock=1
```

With `--preset slow`, `slower` (default), `veryslow` or `placebo`, these will be applied
additionally:

```
ref=6:bframes=8:lookahead-slices=0:rd=4:max-merge=5:tskip=1
```

These options are highly optimized for video quality, I do not recommend changing them unless you
have strong video encoding knowledge.

## Building from source

- Python 3.14 or higher
- [uv](https://github.com/astral-sh/uv)
- `ffmpeg.exe` (details below)

Install dependencies:

```sh
uv sync
```

Run the project:

```sh
uv run main.py USM/7.1/Cs_ZD_AQ70200801_TFR_Boy.usm -vs -nc
```

For flag options, refer to the [Options](#options) section.

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

If you're paranoid about running random executables by strangers (me), use the full official build
from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/#release-builds), which is much bigger.

### Build Command

```sh
uv run pyinstaller charlotte.spec
```

## Credits

- [GI-cutscenes](https://github.com/ToaHartor/GI-cutscenes) for the original decryption
  implementation Charlotte is based on
- [UsmDiviner](https://github.com/Senkin219/UsmDiviner) for inspiring me with the key guessing
  algorithm
- [Dimbreath](https://gitlab.com/Dimbreath/animegamedata2) for subtitles

## Contributing

Help with translation improvements is much
appreciated: [Crowdin](https://crowdin.com/project/charlotte-lang)

If you have missing video keys, pull requests are welcome.

## ❤️ Support

If you enjoyed using Charlotte, your support would mean so much to me. It keeps me motivated to
invest more time into the project and keep it alive for as long as I can.

**[GitHub Sponsors](https://github.com/sponsors/lunarmint)**

## Disclaimer

This tool is purely for educational purposes and aims to archive already released game
content.
