import functools
import re

from typing import TYPE_CHECKING, Any, NamedTuple

import orjson
import urllib3

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import app_root


if TYPE_CHECKING:
    from collections.abc import Callable

    from utils.reporter import Reporter


KEY_MASK = (1 << 56) - 1


class DecryptionKey(NamedTuple):
    """key1 and key2 decrypt both audio + video mask. 7.1+ uses an AES key for the video instead."""

    key1: bytes
    key2: bytes
    aes_key: bytes | None = None


def wrap_key(value: int) -> int:
    return (value & KEY_MASK) or 1 << 56


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

    return wrap_key(sum_val)


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
    except urllib3.exceptions.HTTPError as e:
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


def split_key(key: int) -> tuple[bytes, bytes]:
    key_bytes = key.to_bytes(8, "little")
    return key_bytes[:4], key_bytes[4:]


def parse_stream_keys(audio_key: object, aes_key: object) -> DecryptionKey | None:
    if not (
        isinstance(audio_key, int)
        and 0 <= audio_key <= KEY_MASK
        and isinstance(aes_key, str)
        and re.fullmatch(r"\s*[0-9A-Fa-f]{32}\s*", aes_key)
    ):
        return None
    # 7.1 doesn't use filename anymore so audioKey is used as it is.
    key1, key2 = split_key(audio_key)
    return DecryptionKey(key1, key2, bytes.fromhex(aes_key))


def find_stream_keys(data: dict, filename: str) -> DecryptionKey | None:
    found = find_video(data, filename)
    return parse_stream_keys(found[1].get("audioKey"), found[1].get("aesKey")) if found else None


class Keys:
    def __init__(self, reporter: Reporter, manual_key: str | None = None):
        self.reporter = reporter
        self.manual_key = manual_key
        self.path = app_root() / "keys.json"
        self.data: dict = {}
        self.raw = b""
        self.declined = False
        if manual_key is None:
            self.bootstrap()

    def bootstrap(self) -> None:
        if self.path.exists():
            self.raw = self.path.read_bytes()
        else:
            log.info(f"keys.json not found at {self.path}.")
            self.raw = fetch_upstream_keys() or b""
            if not self.raw:
                log.error("Failed to fetch keys.json. Keys will be retrieved from the file itself.")
                return
            try:
                self.path.write_bytes(self.raw)
            except OSError as e:
                log.warning(f"Failed to save keys.json: {e}")

        try:
            self.data = orjson.loads(self.raw)
        except orjson.JSONDecodeError:
            log.error("Error decoding local keys.json. Upstream is checked when a key is missing.")
            self.data = {}
            self.raw = b""

    def get(self, stem: str) -> int | None:
        if self.manual_key is not None:
            try:
                return int(self.manual_key)
            except ValueError:
                raise CharlotteError(
                    f"{stem} uses the old encryption, so --key must be a decimal videoKey."
                ) from None

        return self.find(stem, find_video_key)

    def stream_keys(self, stem: str) -> DecryptionKey | None:
        if self.manual_key is not None:
            audio_key, _, aes_key = self.manual_key.partition(":")
            audio_key = int(audio_key) if audio_key.strip().isdecimal() else None
            stream_keys = parse_stream_keys(audio_key, aes_key)
            if stream_keys is None:
                raise CharlotteError(
                    f"{stem} uses the 7.1 encryption, so --key must be audioKey:aesKey."
                )
            return stream_keys

        return self.find(stem, find_stream_keys)

    def find(self, stem: str, lookup: Callable[[dict, str], Any]) -> Any:
        found = lookup(self.data, stem)
        if found is not None:
            return found

        if self.declined:
            log.info(f"No keys.json entry for {stem}: the update was declined.")
            return None

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

        found = lookup(upstream_data, stem)
        if found is None:
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
        return found

    def decryption_key(self, stem: str) -> DecryptionKey | None:
        key1 = calculate_key_from_filename(stem)
        key2 = self.get(stem)
        if key2 is None:
            return None

        combined = wrap_key(key1 + key2)
        return DecryptionKey(*split_key(combined))
