import msvcrt
import sys
import time
import zipfile

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import orjson
import urllib3

from utils.errors import CharlotteError
from utils.logger import log
from utils.version import __version__


if TYPE_CHECKING:
    from utils.reporter import Reporter


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str | None = None
    available: bool = False
    url: str | None = None
    notes: str | None = None
    download: str | None = None
    reason: str | None = None


def parse_version(text: str) -> tuple[tuple[int, ...], int, int]:
    """
    1.2.3b1  -> ((1, 2, 3), 1, 1)
    1.2.3b2  -> ((1, 2, 3), 1, 2)
    1.2.3rc1 -> ((1, 2, 3), 2, 1)
    1.2.3    -> ((1, 2, 3), 3, 0)
    """
    phase_rank = {"a": 0, "b": 1, "rc": 2}
    final_rank = 3

    core = text.strip().lstrip("vV")
    # Everything up to the first letter is the dotted release.
    # The rest is an optional pre-release suffix like "b1" or "rc2".
    split = next((i for i, char in enumerate(core) if char.isalpha()), len(core))
    release, prerelease = core[:split], core[split:]

    numbers = tuple(int(part) for part in release.split("."))
    phase = prerelease.rstrip("0123456789")
    number = int(prerelease[len(phase) :] or 0)
    return numbers, phase_rank.get(phase, final_rank), number


def fetch_latest_release() -> dict:
    url = "https://api.github.com/repos/The-Steambird/charlotte/releases/latest"
    headers = {
        "User-Agent": f"charlotte/{__version__}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    log.info("Checking for updates...")
    try:
        response = urllib3.request("GET", url, headers=headers, timeout=10.0)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(f"GitHub unreachable ({e})") from e
    if response.status != 200:
        raise CharlotteError(f"HTTP {response.status}")
    try:
        release = orjson.loads(response.data)
    except orjson.JSONDecodeError as e:
        raise CharlotteError(f"malformed release data ({e})") from e
    if not isinstance(release, dict):
        raise CharlotteError("malformed release data")
    return release


def asset_download_url(release: dict) -> str | None:
    assets = release.get("assets", [])
    for asset in assets:
        if asset.get("name", "").lower().endswith(".zip"):
            return asset.get("browser_download_url")
    return None


def check_for_update() -> UpdateInfo:
    try:
        release = fetch_latest_release()
    except CharlotteError as e:
        return UpdateInfo(current=__version__, reason=str(e))

    latest = release.get("tag_name")
    if not isinstance(latest, str) or not latest:
        return UpdateInfo(current=__version__, reason="no release tag found")
    latest = latest.lstrip("vV")
    try:
        available = parse_version(latest) > parse_version(__version__)
    except ValueError:
        return UpdateInfo(current=__version__, reason="unrecognized release tag")

    return UpdateInfo(
        current=__version__,
        latest=latest,
        available=available,
        url=release.get("html_url"),
        notes=release.get("body"),
        download=asset_download_url(release),
    )


def report_update(reporter: Reporter) -> UpdateInfo:
    info = check_for_update()
    if info.reason is not None:
        log.warning(f"Failed to check for updates: {info.reason}.")
    elif info.available and info.latest:
        link = f" ({info.url})" if info.url else ""
        log.info(f"Update available: {info.current} -> {info.latest}{link}")
    else:
        log.info(f"No updates available. Charlotte {info.current} is up to date.")

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


def is_standalone_exe(json_mode: bool) -> bool:
    return getattr(sys, "frozen", False) and not json_mode


def clear_stale_binary() -> None:
    if not getattr(sys, "frozen", False):
        return
    exe = running_exe()
    stale = exe.with_name(exe.name + ".old")
    try:
        stale.unlink(missing_ok=True)
    except OSError as e:
        log.warning(f"Failed to remove {stale.name}: {e}")


def looks_like_exe(path: Path) -> bool:
    try:
        with open(path, "rb") as file:
            return file.read(2) == b"MZ"
    except OSError:
        return False


def stream_to_file(response: urllib3.BaseHTTPResponse, dest: Path, reporter: Reporter) -> None:
    """Write the streamed response body to `dest`, reporting download progress as it goes."""
    length = response.headers.get("Content-Length")
    total = int(length) if length else None
    with (
        open(dest, "wb") as file,
        reporter.task("download", total, unit="B") as task,
    ):
        downloaded = 0
        for chunk in response.stream(65536):
            file.write(chunk)
            downloaded += len(chunk)
            task.set_completed(downloaded)


def download_bundle(url: str, dest: Path, reporter: Reporter) -> None:
    """Stream the release zip at `url` into `dest`."""
    headers = {"User-Agent": f"charlotte/{__version__}"}
    try:
        with urllib3.request(
            "GET", url, headers=headers, preload_content=False, timeout=60.0
        ) as response:
            if response.status != 200:
                raise CharlotteError(f"Failed to download the update: HTTP {response.status}.")
            stream_to_file(response, dest, reporter)
    except urllib3.exceptions.HTTPError as e:
        raise CharlotteError(f"Failed to download the update: {e}") from e
    except OSError as e:
        raise CharlotteError(f"Failed to write the update to disk: {e}") from e


def engine_member(archive: zipfile.ZipFile) -> str:
    for name in archive.namelist():
        if PurePosixPath(name).name.lower() == "charlotte-cli.exe":
            return name
    raise CharlotteError("The update bundle has no charlotte-cli.exe inside.")


def extract_binary(bundle: Path, dest: Path) -> None:
    try:
        with zipfile.ZipFile(bundle) as archive:
            member = engine_member(archive)
            dest.write_bytes(archive.read(member))
    except zipfile.BadZipFile as e:
        raise CharlotteError(f"The update bundle is not a valid zip: {e}") from e
    except OSError as e:
        raise CharlotteError(f"Failed to unpack the update: {e}") from e
    if not looks_like_exe(dest):
        raise CharlotteError("Downloaded file is not a valid Windows executable.")


def swap_binary(new_file: Path) -> None:
    exe = running_exe()
    stale = exe.with_name(exe.name + ".old")
    try:
        stale.unlink(missing_ok=True)
        exe.rename(stale)
    except OSError as e:
        raise CharlotteError(f"Failed to move the current binary aside: {e}") from e
    try:
        new_file.rename(exe)
    except OSError as e:
        try:
            # Roll back.
            stale.rename(exe)
        except OSError as rollback_error:
            log.error(f"Rollback failed, restore {stale.name} manually: {rollback_error}")
        raise CharlotteError(f"Failed to put the new binary in place: {e}") from e


def apply_update(info: UpdateInfo, reporter: Reporter) -> bool:
    exe = running_exe()
    bundle = exe.with_name(exe.name + ".zip")
    new_file = exe.with_name(exe.name + ".new")
    try:
        if info.download is None:
            raise CharlotteError("The latest release has no .zip asset to download.")
        download_bundle(info.download, bundle, reporter)
        extract_binary(bundle, new_file)
        swap_binary(new_file)
        return True
    except CharlotteError as e:
        log.error(str(e))
        new_file.unlink(missing_ok=True)
        return False
    finally:
        bundle.unlink(missing_ok=True)


def pause_before_exit(seconds: int = 5) -> None:
    for remaining in range(seconds, 0, -1):
        sys.stderr.write(f"\rExiting in {remaining}... (press any key) ")
        sys.stderr.flush()
        for _ in range(10):
            if msvcrt.kbhit():
                msvcrt.getch()
                sys.stderr.write("\n")
                return
            time.sleep(0.1)
    sys.stderr.write("\n")


def run_update(reporter: Reporter, json_mode: bool) -> None:
    info = report_update(reporter)
    if not (info.available and info.latest and is_standalone_exe(json_mode)):
        return

    wants_install = reporter.ask(f"Download and install {info.latest} now?", default=False)
    if wants_install and apply_update(info, reporter):
        log.info(f"Upgraded Charlotte from {info.current} to {info.latest}!")
        log.info("Restart Charlotte to use the new version.")
        pause_before_exit()
