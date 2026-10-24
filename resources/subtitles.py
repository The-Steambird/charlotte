import hashlib
import io
import zipfile

from concurrent.futures import ThreadPoolExecutor
lazy from pathlib import Path

import orjson
lazy import urllib3

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import app_root
from utils.strings import translate
from utils.version import __version__
lazy from utils.http import RETRIES
lazy from utils.reporter import Reporter


MIRROR = "DimbreathBot/AnimeGameData"
PARALLEL_DOWNLOADS = 8

BASENAME_FIXES = {
    "Cs_4131904_HaiDaoChuXian_Boy": "Cs_Activity_4001103_Summertime_Boy",
    "Cs_4131904_HaiDaoChuXian_Girl": "Cs_Activity_4001103_Summertime_Girl",
    "Cs_200211_WanYeXianVideo": "Cs_DQAQ200211_WanYeXianVideo",
}


def subtitle_dir() -> Path:
    return app_root() / "Subtitle"


def local_subtitle_path(stem: str, lang: str) -> Path:
    stem = BASENAME_FIXES.get(stem, stem)
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
        log.warning(translate("SUBTITLE_MARKER_FAILED", error=e))


def is_subtitle(rel: str) -> bool:
    # A ".." segment would let an entry write outside the cache.
    return rel.startswith("Subtitle/") and rel.endswith(".srt") and ".." not in rel.split("/")


def blob_sha(path: Path) -> str | None:
    # Git identifies a file by the SHA-1 of this header and its bytes.
    # A cached file with the same ID is already current.
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def fetch(http: urllib3.PoolManager, url: str, timeout: float = 10.0) -> bytes:
    try:
        response = http.request("GET", url, timeout=timeout)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(str(e)) from e
    if response.status != 200:
        raise CharlotteError(f"HTTP {response.status}")
    return response.data


def gitlab_commit(http: urllib3.PoolManager) -> str:
    url = (
        "https://gitlab.com/api/v4/projects/Dimbreath%2Fanimegamedata2/repository/commits"
        "?path=Subtitle&ref_name=main&per_page=1"
    )
    try:
        return orjson.loads(fetch(http, url))[0]["id"]
    except ValueError, KeyError, IndexError:
        raise CharlotteError(translate("SUBTITLE_RESPONSE_UNKNOWN")) from None


def gitlab_archive(http: urllib3.PoolManager) -> dict[str, bytes]:
    url = (
        "https://gitlab.com/Dimbreath/animegamedata2/-/archive/main/"
        "animegamedata2-main.zip?path=Subtitle"
    )
    data = fetch(http, url, timeout=120.0)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise CharlotteError(translate("SUBTITLE_ARCHIVE_INVALID")) from e

    files = {}
    with archive:
        for name in archive.namelist():
            # animegamedata2-main-Subtitle/Subtitle/<LANG>/<file>.srt
            # Dropping the first part puts everything under the app's Subtitle/.
            _, _, rel = name.partition("/")
            if is_subtitle(rel):
                files[rel] = archive.read(name)
    return files


def github_commit(http: urllib3.PoolManager) -> str:
    url = f"https://api.github.com/repos/{MIRROR}/commits?path=Subtitle&sha=main&per_page=1"
    try:
        return orjson.loads(fetch(http, url))[0]["sha"]
    except ValueError, KeyError, IndexError:
        raise CharlotteError(translate("SUBTITLE_RESPONSE_UNKNOWN")) from None


def github_changes(http: urllib3.PoolManager, commit: str) -> list[str]:
    url = f"https://api.github.com/repos/{MIRROR}/git/trees/{commit}:Subtitle?recursive=1"
    try:
        entries = orjson.loads(fetch(http, url))["tree"]
    except ValueError, KeyError:
        raise CharlotteError(translate("SUBTITLE_RESPONSE_UNKNOWN")) from None

    changed = []
    for entry in entries:
        rel = f"Subtitle/{entry['path']}"
        if entry["type"] != "blob" or not is_subtitle(rel):
            continue
        if blob_sha(app_root() / rel) != entry["sha"]:
            changed.append(rel)
    return changed


def write_subtitles(files: dict[str, bytes], commit: str, reporter: Reporter) -> None:
    try:
        with reporter.task("subtitles", len(files), unit="file") as task:
            for rel, data in files.items():
                target = app_root() / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                task.advance()
    except OSError as e:
        log.warning(translate("SUBTITLE_WRITE_FAILED", error=e))
        return

    write_commit(commit)
    log.info(translate("SUBTITLES_SYNCED", count=len(files), path=subtitle_dir()))


def sync_from_gitlab(http: urllib3.PoolManager, reporter: Reporter) -> None:
    commit = gitlab_commit(http)
    if commit == stored_commit():
        log.debug("Subtitles already up to date.")
        return

    log.info(translate("SUBTITLE_UPDATE_FOUND", source="GitLab"))
    files = gitlab_archive(http)
    if not files:
        log.warning(translate("SUBTITLE_ARCHIVE_EMPTY"))
        return
    write_subtitles(files, commit, reporter)


def sync_from_github(http: urllib3.PoolManager, reporter: Reporter) -> None:
    commit = github_commit(http)
    if commit == stored_commit():
        log.debug("Subtitles already up to date.")
        return

    log.info(translate("SUBTITLE_UPDATE_FOUND", source="GitHub"))
    changed = github_changes(http, commit)

    def download(rel: str) -> bytes:
        return fetch(http, f"https://raw.githubusercontent.com/{MIRROR}/{commit}/{rel}")

    files = {}
    # A few downloads are queued at a time. A failure or a cancel would still wait
    # for remaining files to download.
    with (
        reporter.task("subtitles", len(changed), unit="file") as task,
        ThreadPoolExecutor(PARALLEL_DOWNLOADS) as executor,
    ):
        downloads = executor.map(download, changed, buffersize=PARALLEL_DOWNLOADS)
        for rel, data in zip(changed, downloads, strict=True):
            # The batch's first checkpoint reports the cancel.
            if reporter.cancel_requested():
                return
            files[rel] = data
            task.advance()
    write_subtitles(files, commit, reporter)


def sync_subtitles(reporter: Reporter) -> None:
    """Mirror the upstream Subtitle/ into the local cache when the upstream commit differs
    from Subtitle/.sync.json. GitHub mirror is used when GitLab fails."""
    http = urllib3.PoolManager(
        maxsize=PARALLEL_DOWNLOADS,
        retries=RETRIES,
        headers={"User-Agent": f"charlotte/{__version__}"},
    )
    try:
        sync_from_gitlab(http, reporter)
        return
    except CharlotteError as e:
        log.warning(translate("SUBTITLE_TRYING_MIRROR", error=e))

    try:
        sync_from_github(http, reporter)
    except CharlotteError as e:
        log.warning(translate("SUBTITLE_SYNC_SKIPPED", error=e))
