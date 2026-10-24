"""Finds which of the game's own video files a captured 7.1+ key decrypts.

A VP9 frame that holds several frames ends in a superframe index. The index repeats its marker
byte at both ends and lists sizes that add up to the frame. AES leaves no such structure under a
wrong key. Over the first 300 frames, the right key passed on 26 to 49 and every wrong key on none.
"""

from typing import NamedTuple
lazy from collections.abc import Callable, Iterator
lazy from pathlib import Path

from stages.usm import MASK_START, decrypt_stream, read_chunks, video_nonce
from utils.errors import CharlotteError


class Candidate(NamedTuple):
    path: Path
    nonce: int


class Frame(NamedTuple):
    frame_time: int
    payload: bytes
    start: int
    end: int


def unlisted_stream_files(video_dir: Path, listed: set[str]) -> list[Candidate]:
    """Reading a file's nonce stops at its first video chunk."""
    candidates = []
    for path in sorted(video_dir.glob("*.usm")):
        if path.stem in listed:
            continue
        try:
            nonce = video_nonce(path)
        except CharlotteError, OSError:
            continue
        if nonce is not None:
            candidates.append(Candidate(path, nonce))
    return candidates


def video_sample(path: Path) -> list[Frame]:
    """Frames that end inside the clear part are skipped, because their index would pass under
    any key."""
    payloads = (
        (header.frame_time, payload)
        for header, payload in read_chunks(path)
        if header.signature == b"@SFV" and header.is_data
    )
    sample = []
    for number, (frame_time, payload) in enumerate(payloads):
        if number == 300:
            break
        # Each frame starts with a 12-byte header that holds its length. The first payload also
        # has the 32-byte IVF file header in front of that.
        start = 32 if number == 0 else 0
        frame_at = start + 12
        if len(payload) < frame_at:
            continue
        frame_size = int.from_bytes(payload[start : start + 4], "little")
        end = frame_at + frame_size
        if end <= MASK_START or end > len(payload):
            continue
        sample.append(Frame(frame_time, payload, frame_at, end))
    return sample


def ends_in_superframe_index(frame: bytearray) -> bool:
    marker = frame[-1]
    if marker & 0xE0 != 0xC0:
        return False
    size_bytes = ((marker >> 3) & 3) + 1
    frames = (marker & 7) + 1
    index_size = 2 + size_bytes * frames
    if index_size > len(frame) or frame[-index_size] != marker:
        return False
    sizes = frame[-index_size + 1 : -1]
    total = sum(
        int.from_bytes(sizes[at : at + size_bytes], "little")
        for at in range(0, len(sizes), size_bytes)
    )
    return total == len(frame) - index_size


def decrypts(sample: list[Frame], nonce: int, aes_key: bytes) -> bool:
    passed = 0
    for frame in sample:
        data = bytearray(frame.payload)
        decrypt_stream(data, aes_key, nonce, frame.frame_time)
        if ends_in_superframe_index(data[frame.start : frame.end]):
            passed += 1
            if passed == 3:
                return True
    return False


def match_keys(
    candidates: list[Candidate],
    aes_keys: dict[int, str],
    checkpoint: Callable[[], None],
) -> Iterator[tuple[int, str]]:
    """Yields (audio key, stem) for each candidate that one of the AES keys decrypts."""
    keys = {audio_key: bytes.fromhex(aes_key) for audio_key, aes_key in aes_keys.items()}
    for candidate in candidates:
        checkpoint()
        try:
            sample = video_sample(candidate.path)
        except CharlotteError, OSError:
            continue
        for audio_key, aes_key in keys.items():
            if decrypts(sample, candidate.nonce, aes_key):
                yield audio_key, candidate.path.stem
                break
