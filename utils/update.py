import hashlib
import msvcrt
import sys
import time
import zipfile

from dataclasses import dataclass
from pathlib import Path

import orjson
lazy import urllib3

from utils.errors import Cancelled, CharlotteError
from utils.logger import log
from utils.strings import translate
from utils.version import __version__
lazy from utils.reporter import Reporter


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str | None = None
    available: bool = False
    url: str | None = None
    notes: str | None = None
    download: str | None = None
    sha256: str | None = None
    reason: str | None = None


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def fetch_latest_release() -> dict:
    url = "https://api.github.com/repos/The-Steambird/charlotte/releases/latest"
    headers = {
        "User-Agent": f"charlotte/{__version__}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    log.info(translate("UPDATE_CHECKING"))
    try:
        response = urllib3.request("GET", url, headers=headers, timeout=10.0)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(translate("UPDATE_GITHUB_UNREACHABLE", error=e)) from e
    if response.status != 200:
        raise CharlotteError(f"HTTP {response.status}")
    try:
        return orjson.loads(response.data)
    except orjson.JSONDecodeError as e:
        raise CharlotteError(translate("UPDATE_RELEASE_MALFORMED")) from e


def zip_asset(release: dict) -> dict:
    for asset in release.get("assets", []):
        if asset.get("name", "").lower().endswith(".zip"):
            return asset
    return {}


def check_for_update() -> UpdateInfo:
    try:
        release = fetch_latest_release()
    except CharlotteError as e:
        return UpdateInfo(current=__version__, reason=str(e))

    latest = release["tag_name"].lstrip("vV")
    try:
        available = parse_version(latest) > parse_version(__version__)
    except ValueError:
        return UpdateInfo(current=__version__, reason=translate("UPDATE_TAG_UNRECOGNIZED"))

    asset = zip_asset(release)
    digest = asset.get("digest") or ""
    sha256 = None
    if digest.startswith("sha256:"):
        sha256 = digest.removeprefix("sha256:")
    return UpdateInfo(
        current=__version__,
        latest=latest,
        available=available,
        url=release.get("html_url"),
        notes=release.get("body"),
        download=asset.get("browser_download_url"),
        sha256=sha256,
    )


def report_update(reporter: Reporter) -> UpdateInfo:
    info = check_for_update()
    if info.reason is not None:
        log.warning(translate("UPDATE_CHECK_FAILED", reason=info.reason))
    elif info.available:
        link = f" ({info.url})" if info.url else ""
        log.info(translate("UPDATE_AVAILABLE", current=info.current, latest=info.latest, link=link))
    else:
        log.info(translate("UP_TO_DATE", version=info.current))

    reporter.event(
        "update",
        current=info.current,
        latest=info.latest,
        available=info.available,
        url=info.url,
        notes=info.notes,
        download=info.download,
        reason=info.reason,
    )
    return info


def running_exe() -> Path:
    return Path(sys.executable)


def clear_stale_files() -> None:
    if not getattr(sys, "frozen", False):
        return
    for stale in running_exe().parent.glob("*.old"):
        try:
            stale.unlink()
        except OSError as e:
            log.warning(translate("DELETE_FAILED", name=stale.name, error=e))


def stream_to_file(response: urllib3.BaseHTTPResponse, dest: Path, reporter: Reporter) -> str:
    length = response.headers.get("Content-Length")
    total = int(length) if length else None
    digest = hashlib.sha256()
    with (
        open(dest, "wb") as file,
        reporter.task("download", total, unit="B") as task,
    ):
        downloaded = 0
        for chunk in response.stream(65536):
            if reporter.cancel_requested():
                raise Cancelled
            file.write(chunk)
            digest.update(chunk)
            downloaded += len(chunk)
            task.set_completed(downloaded)
    return digest.hexdigest()


def download_bundle(url: str, dest: Path, reporter: Reporter) -> str:
    headers = {"User-Agent": f"charlotte/{__version__}"}
    try:
        with urllib3.request(
            "GET", url, headers=headers, preload_content=False, timeout=60.0
        ) as response:
            if response.status != 200:
                raise CharlotteError(
                    translate("UPDATE_DOWNLOAD_FAILED", error=f"HTTP {response.status}")
                )
            return stream_to_file(response, dest, reporter)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(translate("UPDATE_DOWNLOAD_FAILED", error=e)) from e
    except OSError as e:
        raise CharlotteError(translate("UPDATE_WRITE_FAILED", error=e)) from e


def bundle_targets(archive: zipfile.ZipFile, folder: Path) -> list[tuple[str, Path]]:
    root = folder.resolve()
    targets = []
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        target = (root / name.removeprefix("charlotte/")).resolve()
        if not target.is_relative_to(root):
            raise CharlotteError(translate("UPDATE_ARCHIVE_ESCAPES", name=name))
        targets.append((name, target))
    return targets


def roll_back(installed: list[tuple[Path, Path | None]]) -> None:
    for target, stale in installed:
        try:
            if stale is None:
                target.unlink(missing_ok=True)
            else:
                stale.replace(target)
        except OSError as e:
            log.error(translate("UPDATE_ROLLBACK_FAILED", name=target.name, error=e))


def install_bundle(bundle: Path, folder: Path) -> None:
    try:
        archive = zipfile.ZipFile(bundle)
    except zipfile.BadZipFile as e:
        raise CharlotteError(translate("UPDATE_ZIP_INVALID", error=e)) from e

    with archive:
        targets = bundle_targets(archive, folder)
        installed = []
        try:
            for name, target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                stale = None
                if target.exists():
                    stale = target.with_name(target.name + ".old")
                    target.replace(stale)
                installed.append((target, stale))
                target.write_bytes(archive.read(name))
        except OSError as e:
            roll_back(installed)
            raise CharlotteError(translate("UPDATE_UNPACK_FAILED", error=e)) from e


def apply_update(info: UpdateInfo, reporter: Reporter) -> None:
    folder = running_exe().parent
    bundle = folder / "charlotte-update.zip"
    try:
        if info.download is None:
            raise CharlotteError(translate("UPDATE_NO_ZIP"))
        if info.sha256 is None:
            raise CharlotteError(translate("UPDATE_NO_DIGEST"))
        actual = download_bundle(info.download, bundle, reporter)
        if actual != info.sha256.lower():
            raise CharlotteError(
                translate("UPDATE_DIGEST_MISMATCH", expected=info.sha256, actual=actual)
            )
        install_bundle(bundle, folder)
    finally:
        bundle.unlink(missing_ok=True)


def key_pressed_within(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            msvcrt.getch()
            return True
        time.sleep(0.1)
    return False


def pause_before_exit(seconds: int = 5) -> None:
    for remaining in range(seconds, 0, -1):
        sys.stderr.write(f"\r{translate('EXIT_COUNTDOWN', seconds=remaining)} ")
        sys.stderr.flush()
        if key_pressed_within(1):
            break
    sys.stderr.write("\n")


def run_update(reporter: Reporter, json_mode: bool) -> None:
    info = report_update(reporter)
    # From source there is no exe to replace.
    if not (info.available and getattr(sys, "frozen", False)):
        return

    wants_install = reporter.ask(
        translate("UPDATE_INSTALL_PROMPT", version=info.latest), default=False
    )
    if not wants_install:
        return

    reason = None
    try:
        apply_update(info, reporter)
    except CharlotteError as e:
        reason = str(e)
    except Cancelled:
        reason = translate("UPDATE_CANCELLED")
    reporter.event("update_install", installed=reason is None, reason=reason)

    if reason is not None:
        log.error(reason)
        return
    log.info(translate("UPDATE_UPGRADED", current=info.current, latest=info.latest))
    # The GUI restarts itself, and only a console window needs holding open to be read.
    if not json_mode:
        log.info(translate("UPDATE_RESTART"))
        pause_before_exit()
