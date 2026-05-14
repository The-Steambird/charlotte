import sys

from pathlib import Path

import orjson
import typer
import urllib3
from utils.logger import log


http = urllib3.PoolManager()


def calculate_key_from_filename(filename: str) -> int:
    """Calculate encryption key component from filename.
    This is the first part of the key calculation, based on a hash of the filename.
    """
    # Handle special intro files that share the same base name.
    intro_files = [
        "MDAQ001_OPNew_Part1",
        "MDAQ001_OPNew_Part2_PlayerBoy",
        "MDAQ001_OPNew_Part2_PlayerGirl",
    ]
    if filename in intro_files:
        filename = "MDAQ001_OP"

    # Calculate hash: sum = char + 3 * sum for each character.
    sum_val = 0
    for char in filename:
        sum_val = ord(char) + 3 * sum_val

    # Mask to 56 bits (0xFFFFFFFFFFFFFF = 2^56 - 1).
    sum_val &= 0xFFFFFFFFFFFFFF

    # Return sum or default value if zero.
    result = 0x100000000000000
    if sum_val > 0:
        result = sum_val

    return result


def fetch_upstream_keys() -> bytes | None:
    """Fetch keys.json from upstream repository to memory."""
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
            return version.get("key", None)

        if "videoGroups" in version:
            for group in version["videoGroups"]:
                if filename in group["videos"]:
                    return group.get("key", None)
    return None


def get_key(filename: str) -> int | None:
    """Find encryption key in keys.json."""
    if getattr(sys, "frozen", False):
        root_dir = Path(sys.executable).parent
    else:
        root_dir = Path(__file__).parent.parent

    keys_path = root_dir / "keys.json"

    # Fetch if completely missing
    if not keys_path.exists():
        log.info(f"keys.json not found at {keys_path}.")
        upstream_data = fetch_upstream_keys()
        if not upstream_data:
            log.error("Failed to fetch keys.json.")
            raise typer.Exit(1)
        keys_path.write_bytes(upstream_data)

    local_bytes = keys_path.read_bytes()
    try:
        local_data = orjson.loads(local_bytes)
    except orjson.JSONDecodeError:
        log.error("Error decoding local keys.json. Attempting to recover from upstream...")
        local_data = {"list": []}
        local_bytes = b""

    # Check local first
    key = find_key_from_file(local_data, filename)
    if key is not None:
        return key

    # Key not found locally, try checking upstream
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
        typer.confirm(
            "New key(s) found. Overwrite local keys.json?",
            default=False,
            abort=True,
        )
        log.info("Resuming demux...")
        try:
            keys_path.write_bytes(upstream_bytes)
        except OSError as e:
            log.warning(f"Could not save keys.json: {e}")
        return new_key

    log.info(f"Key for {filename} not found upstream either.")
    return None


def get_decryption_key(filename: str) -> tuple[bytes, bytes] | None:
    """Get complete decryption key for a USM file.

    Combines the filename-based key with key from keys.json to produce
    the final decryption key split into two 4-byte components.
    """
    # Remove extension if present.
    basename = Path(filename).stem
    key1 = calculate_key_from_filename(basename)
    key2 = get_key(basename)

    if key2 is None:
        return None

    final_key = 0x100000000000000
    if ((key1 + key2) & 0xFFFFFFFFFFFFFF) != 0:
        final_key = (key1 + key2) & 0xFFFFFFFFFFFFFF

    # Split 64-bit key into two 32-bit keys (little-endian).
    key_bytes = final_key.to_bytes(8, byteorder="little")
    key1 = key_bytes[:4]
    key2 = key_bytes[4:]

    return key1, key2
