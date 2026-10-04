import functools
import re

from typing import TYPE_CHECKING, Any, NamedTuple

import orjson
import urllib3

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import app_root
from utils.strings import translate


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
        log.info(translate("KEYS_FETCHING"))
        response = urllib3.request("GET", keys_url, timeout=10.0)
        if response.status == 200:
            log.info(translate("KEYS_FETCHED"))
            return response.data
        log.warning(translate("KEYS_HTTP_ERROR", status=response.status))
    except urllib3.exceptions.HTTPError as e:
        log.error(translate("KEYS_DOWNLOAD_FAILED", error=e))
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
            log.info(translate("KEYS_NOT_FOUND_AT", path=self.path))
            self.raw = fetch_upstream_keys() or b""
            if not self.raw:
                log.error(translate("KEYS_FETCH_FAILED"))
                return
            try:
                self.path.write_bytes(self.raw)
            except OSError as e:
                log.warning(translate("KEYS_SAVE_FAILED", error=e))

        try:
            self.data = orjson.loads(self.raw)
        except orjson.JSONDecodeError:
            log.error(translate("KEYS_LOCAL_INVALID"))
            self.data = {}
            self.raw = b""

    def get(self, stem: str) -> int | None:
        if self.manual_key is not None:
            try:
                return int(self.manual_key)
            except ValueError:
                raise CharlotteError(translate("KEY_NOT_VIDEO_KEY", stem=stem)) from None

        return self.find(stem, find_video_key)

    def stream_keys(self, stem: str) -> DecryptionKey | None:
        if self.manual_key is not None:
            audio_key, _, aes_key = self.manual_key.partition(":")
            audio_key = int(audio_key) if audio_key.strip().isdecimal() else None
            stream_keys = parse_stream_keys(audio_key, aes_key)
            if stream_keys is None:
                raise CharlotteError(translate("KEY_NOT_STREAM_KEYS", stem=stem))
            return stream_keys

        return self.find(stem, find_stream_keys)

    def find(self, stem: str, lookup: Callable[[dict, str], Any]) -> Any:
        found = lookup(self.data, stem)
        if found is not None:
            return found

        if self.declined:
            log.info(translate("KEYS_UPDATE_DECLINED", stem=stem))
            return None

        log.info(translate("KEY_CHECKING_UPSTREAM", stem=stem))
        upstream_bytes = fetch_upstream_keys()
        if not upstream_bytes:
            return None

        if upstream_bytes == self.raw:
            log.info(translate("KEYS_UPSTREAM_IDENTICAL"))
            return None

        try:
            upstream_data = orjson.loads(upstream_bytes)
        except orjson.JSONDecodeError:
            log.error(translate("KEYS_UPSTREAM_INVALID"))
            return None

        found = lookup(upstream_data, stem)
        if found is None:
            log.info(translate("KEY_NOT_UPSTREAM", stem=stem))
            return None

        overwrite_prompt = self.reporter.ask(translate("KEYS_UPDATE_PROMPT"), default=False)
        if not overwrite_prompt:
            self.declined = True
            log.info(translate("KEYS_UPDATE_DECLINED", stem=stem))
            return None

        try:
            self.path.write_bytes(upstream_bytes)
        except OSError as e:
            log.warning(translate("KEYS_SAVE_FAILED", error=e))

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
