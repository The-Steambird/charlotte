<p align="center">
  <img width="2100px" height="auto" src="https://raw.githubusercontent.com/lunarmint/charlotte/master/docs/imgs/banner.png"/><br/>
  <i>Hi there! I'm Charlotte, a journalist with The Steambird~</i>
  <i></i>
</p>

##### Artist: [黒光りとまと(御仕事募集中)](https://www.pixiv.net/en/artworks/117728570)

# Charlotte

A program for Genshin Impact that losslessly decrypts the `.usm` cutscene files into playable `.mkv` videos. Supports official audio (EN, CN, JP, KR) and subtitles in 15 languages, with [VapourSynth](https://github.com/vapoursynth/vapoursynth) (a lossless video processing framework) suport to improve video artifacts such as banding, blocking, and chroma aberration that exist in the original cutscenes.

All cutscenes from versions 1.0 to 6.3 can be decrypted.

Feel free to submit a pull request if you have missing keys.

## Why Charlotte?

Who do you think recorded all those cutscenes but Teyvat's best journalist?

This tool was heavily inspired by [GI-cutscenes](https://github.com/ToaHartor/GI-cutscenes). All the decrypt algorithm is based off of this project. GI-cutscene has not been in active development for some time now, so I wanted to rewrite it at higher level and add features down the road such as VapourSynth processing and a GUI.

## Features and Roadmap

- [x] Decrypt `.usm` files into `.ivf` video and `.hca` audio.
- [x] Subtitle support, formatting `.srt` into `.ass` format.
- [x] Font and subtitle style match the official cutscenes.
- [x] `.hca` audio to `.flac` for archival purposes.
- [x] Mux video and audio into `.mkv`.
- [ ] Add VapourSynth processing.
- [ ] Add GUI.

## Running Charlotte

### Prerequisites
- Download [charlotte.exe](https://github.com/lunarmint/charlotte/releases/latest) from the latest release.
- Ensure that [ffmpeg.exe](https://ffmpeg.org/download.html#build-windows) and [mkvmerge.exe](https://mkvtoolnix.download/downloads.html#windows) are present in the same directory as `charlotte.exe`.
- Clone [this repository](https://gitlab.com/Dimbreath/AnimeGameData) and place the `Subtitle` directory in the same directory as `charlotte.exe`.
- To get font files, go to `[Game Directory]\Genshin Impact game\GenshinImpact_Data\StreamingAssets\MiHoYoSDKRes\HttpServerResources` and copy the `font` directory into the same directory as `charlotte.exe`.
- For `.usm` cutscene files, go to `[Game Directory]\Genshin Impact game\GenshinImpact_Data\StreamingAssets\VideoAssets\StandaloneWindows64`. Depending on when you started playing the game and how often you cleaned up past resources or reinstalling the game, not all cutscene files may be available, especially the ones from past limited events. I currently have a full archive of them and will try my best to organize them into a spreadsheet and find a host (~42.3 GB in total).

### Usage
- Run `charlotte.exe` and select the `.usm` file or directory containing `.usm` files.

```sh
charlotte [PATH_TO_USM_FILE_OR_DIR] [OPTIONS]
```

### Example

```sh
charlotte C:\Users\Mint\Desktop\charlotte\USM\Cs_EQHDJ005_HaiDengJie_Boy.usm -nc
```

This will decrypt `Cs_EQHDJ005_HaiDengJie_Boy.usm` and output the result to `output/Cs_EQHDJ005_HaiDengJie_Boy.mkv` without cleaning up intermediate files.

### Arguments

- `PATH_TO_USM_FILE_OR_DIR`: **(Required)** Path to a single `.usm` file or a directory containing `.usm` files.

### Options

- `-o, --output [DIR]`: Output directory. Default is `output`.
- `-nc, --no-cleanup`: Do not delete intermediate decoded files (e.g., `.ivf`, `.hca`, `.ass`) after the process is complete.

## Building from Source

### Prerequisites

- Python 3.14 or higher.
- [uv](https://github.com/astral-sh/uv) package manager.
- Install [VapourSynth](https://www.vapoursynth.com/doc/installation.html#windows-installation).
- Add the VapourSynth installation directory to your PATH environment variable. 

When done, you should be able to run `vspipe --version` from a command prompt. Then, navigate to `[VapourSynth Directory]\vsrepo` and run `python vsrepo.py install bs lsmas` to install the necessary VapourSynth plugins.

### How to Build

```sh
uv run pyinstaller charlotte.spec
```

## Support the Project

I put in a lot of time and effort to make this tool. If you enjoyed using it, your support would mean so much to me. It keeps me motivated to invest more time into the project and keep it alive for as long as I can ❤️

**[GitHub Sponsors](https://github.com/sponsors/lunarmint)**
