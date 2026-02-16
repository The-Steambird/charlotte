import sys
import tempfile

from pathlib import Path
from urllib import request

import orjson
import typer


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


def get_upstream_keys(target_path: Path) -> bool:
    """Fetch keys.json from upstream repository."""
    keys_url = "https://raw.githubusercontent.com/lunarmint/charlotte/refs/heads/master/keys.json"
    try:
        typer.echo("Attempting to fetch keys.json from upstream...")
        with request.urlopen(keys_url) as response:
            if response.status == 200:
                data = response.read()
                target_path.write_bytes(data)
                typer.echo("Successfully updated keys.json.")
                return True
    except Exception as e:
        typer.echo(f"Failed to download keys.json: {e}")
    return False


def find_key_from_file(data: dict, filename: str) -> int | None:
    for version in data["list"]:
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

    keys = root_dir.joinpath("keys.json")

    if not keys.exists():
        typer.echo(f"keys.json not found at {keys}.")
        if not get_upstream_keys(keys):
            typer.echo("Failed to fetch keys.json.")
            raise typer.Exit(1)

    try:
        data = orjson.loads(keys.read_bytes())
        key = find_key_from_file(data, filename)
        if key is not None:
            return key

        # Key not found locally, try checking upstream
        typer.echo(f"Key for {filename} not found. Checking upstream...")

        # Download to a temporary file first
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            temp_path = Path(tmp_file.name)

        try:
            if get_upstream_keys(temp_path):
                # Check if the new file actually has the key or is different.
                retry_data = orjson.loads(temp_path.read_bytes())
                new_key = find_key_from_file(retry_data, filename)
                # Check if content is different.
                if retry_data != data:
                    if new_key:
                        typer.confirm(
                            "New key(s) found. Overwrite local keys.json?",
                            default=False,
                            abort=True,
                        )
                        typer.echo("Resuming demux...")
                        keys.write_bytes(temp_path.read_bytes())
                        return new_key
                else:
                    typer.echo(
                        "Upstream keys.json is identical to local file. Please check back later "
                        "when new keys are available!"
                    )
        finally:
            if temp_path.exists():
                temp_path.unlink()

    except orjson.JSONDecodeError:
        typer.echo("Error decoding keys.json.")

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

    final_key = 0x100000000000000
    if ((key1 + key2) & 0xFFFFFFFFFFFFFF) != 0:
        final_key = (key1 + key2) & 0xFFFFFFFFFFFFFF

    # Split 64-bit key into two 32-bit keys (little-endian).
    key_bytes = final_key.to_bytes(8, byteorder="little")
    key1 = key_bytes[:4]
    key2 = key_bytes[4:]

    return key1, key2
