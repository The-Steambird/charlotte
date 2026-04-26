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

All known cutscenes from versions 1.0 through 6.4 can be decrypted.

If you have missing keys, pull requests are welcome.

## Why Charlotte

Who else would archive Teyvat's cutscenes but its best journalist?

This project is heavily inspired by [GI-cutscenes](https://github.com/ToaHartor/GI-cutscenes). Charlotte rebuilds the workflow at a higher level and aims to add extras over time, including VapourSynth processing and a GUI.

## Features

- [x] Decrypt `.usm` into `.ivf` video and `.hca` audio
- [x] Convert `.srt` subtitles into styled `.ass`
- [x] Match official cutscene subtitle style and fonts
- [x] Convert `.hca` audio to `.flac` for archival
- [x] Mux tracks into `.mkv`
- [x] Add full VapourSynth processing workflow
- [ ] Add GUI

VapourSynth filter scripts take a lot of time to write to ensure quality, hence they will be slowly added over time. If you have encoding knowledge, contributions are welcome!

## Quick Start (Windows Binary)

### Prerequisites

1. Download `charlotte.exe` from the [latest release](https://github.com/lunarmint/charlotte/releases/latest).
2. Put `ffmpeg.exe` and `mkvmerge.exe` in the same directory as `charlotte.exe`.
3. Clone [AnimeGameData](https://gitlab.com/Dimbreath/AnimeGameData) and copy its `Subtitle` folder beside `charlotte.exe`.
4. Locate `.usm` files at:
   - `[Game Directory]\Genshin Impact game\GenshinImpact_Data\StreamingAssets\VideoAssets\StandaloneWindows64`

Note: availability of older cutscenes depends on your local game files and resource cleanup history.

### Usage

```sh
charlotte demux [PATH_TO_USM_FILE_OR_DIR] [OPTIONS]
```

### Example

```sh
charlotte demux C:\Users\Mint\Desktop\charlotte\USM\Cs_EQHDJ005_HaiDengJie_Boy.usm
```

This decrypts the cutscene and writes:

`output/Cs_EQHDJ005_HaiDengJie_Boy/Cs_EQHDJ005_HaiDengJie_Boy.mkv`

### Parameters

| Type | Flag | Alias | Description |
| --- | --- | --- | --- |
| Argument | `PATH_TO_USM_FILE_OR_DIR` | `-` | Path to one `.usm` file or a directory containing `.usm` files. |
| Option | `--output [DIR]` | `-o` | Output directory (default: `output`). |
| Option | `--no-cleanup` | `-nc` | Keep intermediate files (`.ivf`, `.hca`, `.ass`, etc.). |
| Option | `--vapoursynth` | `-vs` | Apply a matching VapourSynth `.vpy` filter script. |
| Option | `--x265-params [PARAMS]` | `-` | Pass custom x265 params (colon-separated). |

## Build From Source

### Prerequisites

- Python `3.14+`
- [uv](https://github.com/astral-sh/uv)
- Required VapourSynth plugins and ML models:
  - Place [adaptivegrain_rs.dll](https://github.com/Irrational-Encoding-Wizardry/adaptivegrain/releases/latest/download/adaptivegrain_rs.dll) in `.venv\Lib\site-packages\vapoursynth\plugins\vsrepo`
  - Place [ArtCNN_R8F64.onnx](https://github.com/Artoriuz/ArtCNN/releases/latest/download/ArtCNN_R8F64.onnx) in `.venv\Lib\site-packages\vapoursynth\plugins\vsrepo\models`
  - Extract `vsmlrt` [part 1](https://github.com/AmusementClub/vs-mlrt/releases/download/v15.16/vsmlrt-windows-x64-cuda.v15.16.7z.001) & [part 2](https://github.com/AmusementClub/vs-mlrt/releases/download/v15.16/vsmlrt-windows-x64-cuda.v15.16.7z.002) to `.venv\Lib\site-packages\vapoursynth\plugins\vsrepo`. To save space, you may remove all other `\models` included with `vsmlrt` except `ArtCNN_R8F64.onnx`.

- Install the rest of the plugins:
    ```sh
    vsrepo install bs dfttest2 akarin mv bm3dcuda_rtc nlm_cuda vszip eedi3m resize2 zsmooth placebo noise vsmlrt_script
    ```
- Copy the game `font` directory from:
   ```
  [Game Directory]\Genshin Impact game\GenshinImpact_Data\StreamingAssets\MiHoYoSDKRes\HttpServerResources
  ```

### Build Command

```sh
uv run pyinstaller charlotte.spec
```

## ❤️ Support

If you enjoyed using Charlotte, your support would mean so much to me. It keeps me motivated to invest more time into the project and keep it alive for as long as I can.

**[GitHub Sponsors](https://github.com/sponsors/lunarmint)**
