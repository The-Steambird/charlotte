import sys

from pathlib import Path

import urllib3

from utils.logger import log


http = urllib3.PoolManager()


def fetch_subtitle(stem: str, lang: str) -> bytes | None:
    url = (
        f"https://gitlab.com/Dimbreath/AnimeGameData/-/raw/master/Subtitle/{lang}/{stem}_{lang}.srt"
    )

    try:
        log.debug(f"Fetching {stem}_{lang}.srt from upstream...")
        response = http.request("GET", url, timeout=10.0)
        if response.status == 200:
            log.debug(f"Successfully fetched {stem}_{lang}.srt.")
            return response.data
        if response.status == 404:
            log.debug(f"{stem}_{lang}.srt not found upstream (404).")
        else:
            log.warning(f"HTTP Error {response.status} while fetching {stem}_{lang}.srt.")
    except urllib3.exceptions.HTTPError as e:
        log.warning(f"Failed to fetch from upstream for {stem}_{lang}.srt: {e}")
    except Exception as e:
        log.error(f"Failed to download subtitle {stem}_{lang}.srt: {e}")

    return None


def get_subtitle_path(stem: str, lang: str) -> Path | None:
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).parent
    else:
        root = Path(__file__).parent.parent

    input_path = root / "Subtitle" / lang
    subtitle_path = input_path / f"{stem}_{lang}.srt"
    if subtitle_path.exists():
        return subtitle_path

    upstream_data = fetch_subtitle(stem, lang)
    if upstream_data:
        try:
            input_path.mkdir(parents=True, exist_ok=True)
            subtitle_path.write_bytes(upstream_data)
            return subtitle_path
        except OSError as e:
            log.error(f"Failed to save subtitle to {subtitle_path}: {e}")

    return None
