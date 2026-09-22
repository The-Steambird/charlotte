import functools

from typing import TYPE_CHECKING

import orjson
import urllib3

from utils.logger import log
from utils.paths import app_root


if TYPE_CHECKING:
    from pathlib import Path

    from utils.reporter import Reporter


def keys_path() -> Path:
    return app_root() / "keys.json"


def calculate_key_from_filename(filename: str) -> int:
    filename_fix = [
        "MDAQ001_OPNew_Part1",
        "MDAQ001_OPNew_Part2_PlayerBoy",
        "MDAQ001_OPNew_Part2_PlayerGirl",
    ]
    if filename in filename_fix:
        filename = "MDAQ001_OP"

    sum_val = 0
    for char in filename:
        sum_val = ord(char) + 3 * sum_val

    return (sum_val & 0xFFFFFFFFFFFFFF) or 0x100000000000000


@functools.cache
def fetch_upstream_keys() -> bytes | None:
    keys_url = (
        "https://raw.githubusercontent.com/The-Steambird/charlotte/refs/heads/master/keys.json"
    )
    try:
        log.info("Attempting to fetch keys.json from upstream...")
        response = urllib3.request("GET", keys_url, timeout=10.0)
        if response.status == 200:
            log.info("Successfully fetched keys.json.")
            return response.data
        log.warning(f"HTTP Error {response.status} while fetching keys.json.")
    except Exception as e:
        log.error(f"Failed to download keys.json: {e}")
    return None


def find_video(data: dict, filename: str) -> tuple[dict, dict] | None:
    for version in data.get("list", []):
        for group in [version, *version.get("videoGroups", [])]:
            if filename in group.get("videos", []):
                return version, group
    return None


def find_video_key(data: dict, filename: str) -> int | None:
    found = find_video(data, filename)
    return found[1].get("videoKey") if found else None


def find_video_version(data: dict, filename: str) -> str | None:
    found = find_video(data, filename)
    return found[0].get("version") if found else None


class Keys:
    def __init__(self, reporter: Reporter, manual_key: int | None = None):
        self.reporter = reporter
        self.manual_key = manual_key
        self.path = keys_path()
        self.data: dict = {}
        self.raw = b""
        self.declined = False
        if manual_key is None:
            self.bootstrap()

    def bootstrap(self) -> None:
        if not self.path.exists():
            log.info(f"keys.json not found at {self.path}.")
            upstream_bytes = fetch_upstream_keys()
            if not upstream_bytes:
                log.error("Failed to fetch keys.json. Keys will be retrieved from the file itself.")
                return
            self.path.write_bytes(upstream_bytes)

        self.raw = self.path.read_bytes()
        try:
            self.data = orjson.loads(self.raw)
        except orjson.JSONDecodeError:
            log.error("Error decoding local keys.json. Upstream is checked when a key is missing.")
            self.data = {}
            self.raw = b""

    def get(self, stem: str) -> int | None:
        if self.manual_key is not None:
            return self.manual_key

        key = find_video_key(self.data, stem)
        if key is not None:
            return key

        if self.declined:
            log.info(f"No keys.json entry for {stem}: the update was declined.")
            return None

        return self.key_from_upstream(stem)

    def key_from_upstream(self, stem: str) -> int | None:
        log.info(f"Key for {stem} not found. Checking upstream...")
        upstream_bytes = fetch_upstream_keys()
        if not upstream_bytes:
            return None

        if upstream_bytes == self.raw:
            log.info("Upstream keys.json is identical to local file.")
            return None

        try:
            upstream_data = orjson.loads(upstream_bytes)
        except orjson.JSONDecodeError:
            log.error("Error decoding upstream keys.json.")
            return None

        new_key = find_video_key(upstream_data, stem)
        if new_key is None:
            log.info(f"Key for {stem} not found upstream either.")
            return None

        overwrite_prompt = self.reporter.ask(
            "Some local keys are missing, but an updated key list is available. Update now?",
            default=False,
        )
        if not overwrite_prompt:
            self.declined = True
            log.info(f"No keys.json entry for {stem}: the update was declined.")
            return None

        try:
            self.path.write_bytes(upstream_bytes)
        except OSError as e:
            log.warning(f"Failed to save keys.json: {e}")

        self.data = upstream_data
        self.raw = upstream_bytes
        return new_key

    def decryption_key(self, stem: str) -> tuple[bytes, bytes] | None:
        key1 = calculate_key_from_filename(stem)
        key2 = self.get(stem)
        if key2 is None:
            return None

        final_key = ((key1 + key2) & 0xFFFFFFFFFFFFFF) or 0x100000000000000
        key_bytes = final_key.to_bytes(8, byteorder="little")
        return key_bytes[:4], key_bytes[4:]
