import functools

from pathlib import Path
from typing import TYPE_CHECKING

import orjson
import urllib3

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import app_root


if TYPE_CHECKING:
    from utils.reporter import Reporter


http = urllib3.PoolManager()


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

    sum_val &= 0xFFFFFFFFFFFFFF
    result = 0x100000000000000
    if sum_val > 0:
        result = sum_val

    return result


@functools.cache
def fetch_upstream_keys() -> bytes | None:
    """Fetch keys.json from upstream repository to memory. Cached: at most one request
    per run, even when a whole batch is missing keys."""
    keys_url = "https://raw.githubusercontent.com/lunarmint/charlotte/refs/heads/master/keys.json"
    try:
        log.info("Attempting to fetch keys.json from upstream...")
        response = http.request("GET", keys_url, timeout=10.0)
        if response.status == 200:
            log.info("Successfully fetched keys.json.")
            return response.data
        log.warning(f"HTTP Error {response.status} while fetching keys.json.")
    except urllib3.exceptions.HTTPError as e:
        log.error(f"Failed to connect to upstream to fetch keys.json: {e}")
    except Exception as e:
        log.error(f"Failed to download keys.json: {e}")
    return None


def find_key_from_file(data: dict, filename: str) -> int | None:
    for version in data.get("list", []):
        if "videos" in version and filename in version["videos"]:
            return version.get("videoKey", None)

        if "videoGroups" in version:
            for group in version["videoGroups"]:
                if filename in group["videos"]:
                    return group.get("videoKey", None)
    return None


def load_local_keys() -> dict:
    """Read-only parse of the local keys.json for probing; empty when missing or
    corrupt. Never fetches, prompts, or writes - that is get_key's job."""
    try:
        return orjson.loads((app_root() / "keys.json").read_bytes())
    except OSError, orjson.JSONDecodeError:
        return {}


def get_key(filename: str, reporter: Reporter) -> int | None:
    keys_path = app_root() / "keys.json"

    # Fetch if completely missing.
    if not keys_path.exists():
        log.info(f"keys.json not found at {keys_path}.")
        upstream_data = fetch_upstream_keys()
        if not upstream_data:
            log.error("Failed to fetch keys.json.")
            raise CharlotteError("Failed to fetch keys.json.")
        keys_path.write_bytes(upstream_data)

    local_bytes = keys_path.read_bytes()
    try:
        local_data = orjson.loads(local_bytes)
    except orjson.JSONDecodeError:
        log.error("Error decoding local keys.json. Attempting to recover from upstream...")
        local_data = {"list": []}
        local_bytes = b""

    # Check local first.
    key = find_key_from_file(local_data, filename)
    if key is not None:
        return key

    # Key not found locally, try checking upstream.
    log.info(f"Key for {filename} not found. Checking upstream...")
    upstream_bytes = fetch_upstream_keys()

    if not upstream_bytes or upstream_bytes == local_bytes:
        log.info(
            "Upstream keys.json is identical to local file. Please check back later "
            "when new keys are available!"
        )
        return None

    try:
        upstream_data = orjson.loads(upstream_bytes)
    except orjson.JSONDecodeError:
        log.error("Error decoding upstream keys.json.")
        return None

    new_key = find_key_from_file(upstream_data, filename)
    if new_key is not None:
        if reporter.ask("New key(s) found. Overwrite local keys.json?", default=False):
            try:
                keys_path.write_bytes(upstream_bytes)
            except OSError as e:
                log.warning(f"Could not save keys.json: {e}")
            return new_key
        log.info(f"Skipping {filename}: key update declined.")
        return None

    log.info(f"Key for {filename} not found upstream either.")
    return None


def get_decryption_key(filename: str, reporter: Reporter) -> tuple[bytes, bytes] | None:
    basename = Path(filename).stem
    key1 = calculate_key_from_filename(basename)
    key2 = get_key(basename, reporter)

    if key2 is None:
        return None

    final_key = (key1 + key2) & 0xFFFFFFFFFFFFFF
    if final_key == 0:
        final_key = 0x100000000000000

    key_bytes = final_key.to_bytes(8, byteorder="little")
    return key_bytes[:4], key_bytes[4:]
