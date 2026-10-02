import io
import zipfile

from typing import TYPE_CHECKING

import orjson
import urllib3

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import app_root
from utils.strings import translate


if TYPE_CHECKING:
    from pathlib import Path

    from utils.reporter import Reporter


SUBTITLE_ARCHIVE_URL = (
    "https://gitlab.com/Dimbreath/animegamedata2/-/archive/main/"
    "animegamedata2-main.zip?path=Subtitle"
)

SUBTITLE_COMMITS_URL = (
    "https://gitlab.com/api/v4/projects/Dimbreath%2Fanimegamedata2/repository/commits"
    "?path=Subtitle&ref_name=main&per_page=1"
)


def subtitle_dir() -> Path:
    return app_root() / "Subtitle"


def local_subtitle_path(stem: str, lang: str) -> Path:
    return subtitle_dir() / lang / f"{stem}_{lang}.srt"


def sync_marker() -> Path:
    # Delete this file to force subtitle re-sync.
    return subtitle_dir() / ".sync.json"


def stored_commit() -> str:
    try:
        return orjson.loads(sync_marker().read_text()).get("commit", "")
    except OSError, ValueError:
        return ""


def write_commit(commit: str) -> None:
    try:
        sync_marker().write_bytes(orjson.dumps({"commit": commit}))
    except OSError as e:
        log.warning(f"Failed to write subtitle sync marker: {e}")


def latest_commit() -> str:
    try:
        response = urllib3.request("GET", SUBTITLE_COMMITS_URL, timeout=10.0)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(translate("SUBTITLE_CHECK_FAILED", error=e)) from e

    if response.status != 200:
        raise CharlotteError(translate("SUBTITLE_CHECK_FAILED", error=f"HTTP {response.status}"))

    try:
        return orjson.loads(response.data)[0]["id"]
    except ValueError, KeyError, IndexError:
        raise CharlotteError(translate("SUBTITLE_RESPONSE_UNKNOWN")) from None


def fetch_archive() -> zipfile.ZipFile:
    try:
        response = urllib3.request("GET", SUBTITLE_ARCHIVE_URL, timeout=120.0)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(translate("SUBTITLE_DOWNLOAD_FAILED", error=e)) from e

    if response.status != 200:
        raise CharlotteError(translate("SUBTITLE_DOWNLOAD_FAILED", error=f"HTTP {response.status}"))

    try:
        return zipfile.ZipFile(io.BytesIO(response.data))
    except zipfile.BadZipFile as e:
        raise CharlotteError(translate("SUBTITLE_ARCHIVE_INVALID")) from e


def sync_subtitles(reporter: Reporter) -> None:
    """Mirror the upstream Subtitle/ folder into the local cache when the upstream commit differs
    from the one in Subtitle/.sync.json."""
    try:
        latest = latest_commit()
        if latest == stored_commit():
            log.debug("Subtitles already up to date.")
            return

        log.info("Subtitle update found, downloading archive from GitLab...")
        archive = fetch_archive()
    except CharlotteError as e:
        log.warning(f"Skipping subtitle sync: {e}. Using local cache.")
        return

    with archive:
        # animegamedata2-main-Subtitle/Subtitle/<LANG>/<file>.srt -> drop the top-level prefix
        # dir so files land in <root>/Subtitle/<LANG>/...
        root = app_root()
        targets = []
        for name in archive.namelist():
            _, _, rel = name.partition("/")
            # Only .srt leaves, and guard against zip-slip (../ escaping the cache).
            if rel.startswith("Subtitle/") and rel.endswith(".srt") and ".." not in rel.split("/"):
                targets.append((name, root / rel))

        if not targets:
            log.warning("Subtitle archive contained no subtitles, using local cache.")
            return

        try:
            with reporter.task("subtitles", len(targets), unit="file") as task:
                for name, target in targets:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
                    task.advance()
        except OSError as e:
            log.warning(f"Failed to write subtitles, the next run retries: {e}")
            return

    write_commit(latest)
    log.info(f"Synced {len(targets)} subtitle file(s) into {subtitle_dir()}.")
