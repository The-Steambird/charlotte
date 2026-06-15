import io
import json
import zipfile

from typing import TYPE_CHECKING

import urllib3

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import app_root


if TYPE_CHECKING:
    from pathlib import Path

    from utils.reporter import Reporter


http = urllib3.PoolManager()


SUBTITLE_ARCHIVE_URL = (
    "https://gitlab.com/Dimbreath/AnimeGameData/-/archive/master/"
    "AnimeGameData-master.zip?path=Subtitle"
)

SUBTITLE_COMMITS_URL = (
    "https://gitlab.com/api/v4/projects/Dimbreath%2FAnimeGameData/repository/commits"
    "?path=Subtitle&ref_name=master&per_page=1"
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
        data = json.loads(sync_marker().read_text())
        return data.get("commit", "") if isinstance(data, dict) else ""
    except OSError, ValueError:
        return ""


def write_commit(commit: str) -> None:
    try:
        sync_marker().write_text(json.dumps({"commit": commit}))
    except OSError as e:
        log.warning(f"Failed to write subtitle sync marker: {e}")


def latest_commit() -> str:
    try:
        response = http.request("GET", SUBTITLE_COMMITS_URL, timeout=10.0)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(f"Could not check for updates: {e}") from e

    if response.status != 200:
        raise CharlotteError(f"Could not check for updates (HTTP {response.status}).")

    try:
        return json.loads(response.data)[0]["id"]
    except ValueError, KeyError, IndexError:
        raise CharlotteError("Unknown response from subtitle upstream.") from None


def fetch_archive() -> zipfile.ZipFile:
    try:
        response = http.request("GET", SUBTITLE_ARCHIVE_URL, timeout=120.0)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(f"Download failed: {e}") from e

    if response.status != 200:
        raise CharlotteError(f"Download failed (HTTP {response.status}).")

    try:
        return zipfile.ZipFile(io.BytesIO(response.data))
    except zipfile.BadZipFile as e:
        raise CharlotteError("Archive is not a valid zip.") from e


def extract_member(archive: zipfile.ZipFile, name: str, target: Path) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as src:
            target.write_bytes(src.read())
        return True
    except OSError as e:
        log.warning(f"Failed to write {target.name}: {e}")
        return False


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
        # AnimeGameData-master-Subtitle/Subtitle/<LANG>/<file>.srt -> drop the top-level prefix
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

        written = 0
        with reporter.task("subtitles", len(targets), unit="file") as task:
            for name, target in targets:
                if extract_member(archive, name, target):
                    written += 1
                task.advance()

    # Mark the commit when every file landed. A partial write re-downloads next run.
    if written == len(targets):
        write_commit(latest)

    log.info(f"Synced {written} subtitle file(s) into {subtitle_dir()}.")
    if written < len(targets):
        log.warning(f"{len(targets) - written} subtitle file(s) failed to write.")
