import struct

from contextlib import ExitStack, closing
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import numpy as np

from utils.errors import CharlotteError
from utils.logger import log


if TYPE_CHECKING:
    from collections.abc import Generator
    from io import BufferedWriter

    from utils.reporter import Reporter


# Every chunk starts with a 32-byte header. The payload begins data_offset bytes past byte 8.
HEADER_SIZE = 32
MIN_DATA_OFFSET = 0x18  # the header itself already covers this much past byte 8

# A video payload is masked in two regions. If fewer than MIN_MASKED bytes follow the
# clear part, the payload is left alone entirely.
#
#   0x00       0x40        0x140                       end
#    |  clear   |   head    |   chained body ...         |
BLOCK = 0x20
MASK_START = 0x40
CIPHER_START = 0x140
HEAD_SIZE = CIPHER_START - MASK_START
MIN_MASKED = 0x200


def is_masked(payload_size: int) -> bool:
    return payload_size - MASK_START >= MIN_MASKED


class ChunkHeader(NamedTuple):
    signature: bytes
    data_size: int
    data_offset: int
    padding_size: int
    channel_no: int
    data_type: int

    @classmethod
    def from_bytes(cls, raw: bytes) -> ChunkHeader:
        return cls._make(struct.unpack(">4s I x B H B 2x B 16x", raw))


def read_chunks(file_path: Path) -> Generator[tuple[ChunkHeader, bytes]]:
    file_size = file_path.stat().st_size
    with open(file_path, "rb") as fp:
        while True:
            raw = fp.read(HEADER_SIZE)
            if len(raw) < HEADER_SIZE:
                return

            header = ChunkHeader.from_bytes(raw)
            payload_size = header.data_size - header.data_offset - header.padding_size
            # A data_offset inside the header would seek back and re-parse it as chunks.
            if payload_size < 0 or header.data_offset < MIN_DATA_OFFSET:
                raise CharlotteError(f"Corrupt USM chunk: {file_path.name}")

            fp.seek(header.data_offset - MIN_DATA_OFFSET, 1)
            # The payload size is checked before the read because read() allocates the
            # declared size up front, and a corrupt size could ask for 4 GB. A short read
            # would also just end the walk and leave a truncated .ivf behind as if it
            # were whole.
            if payload_size > file_size - fp.tell():
                raise CharlotteError(f"Truncated USM chunk: {file_path.name}")

            payload = fp.read(payload_size)
            fp.seek(header.padding_size, 1)
            yield header, payload


def uses_stream_cipher(file_path: Path) -> bool:
    """7.1 introduces a new cipher adds a `nonce` column to VIDEO_HDRINFO
     and the column name sits in the @UTF table's string pool."""
    with closing(read_chunks(file_path)) as chunks:
        for header, payload in chunks:
            if header.signature == b"@SFV":
                return header.data_type & 0x3 == 1 and b"\x00nonce\x00" in payload
    return False


class USM:
    def __init__(self, file_path: Path, key1: bytes, key2: bytes):
        self.file_path = Path(file_path)
        self.video_mask1 = self.build_mask(key1, key2)
        self.video_mask2 = bytes(b ^ 0xFF for b in self.video_mask1)

    @staticmethod
    def build_mask(key1: bytes, key2: bytes) -> bytes:
        m = bytearray(0x20)

        m[0x00] = key1[0]
        m[0x01] = key1[1]
        m[0x02] = key1[2]
        m[0x03] = (key1[3] - 0x34) & 0xFF
        m[0x04] = (key2[0] + 0xF9) & 0xFF
        m[0x05] = (key2[1] ^ 0x13) & 0xFF
        m[0x06] = (key2[2] + 0x61) & 0xFF
        m[0x07] = (m[0x00] ^ 0xFF) & 0xFF
        m[0x08] = (m[0x02] + m[0x01]) & 0xFF
        m[0x09] = (m[0x01] - m[0x07]) & 0xFF
        m[0x0A] = (m[0x02] ^ 0xFF) & 0xFF
        m[0x0B] = (m[0x01] ^ 0xFF) & 0xFF
        m[0x0C] = (m[0x0B] + m[0x09]) & 0xFF
        m[0x0D] = (m[0x08] - m[0x03]) & 0xFF
        m[0x0E] = (m[0x0D] ^ 0xFF) & 0xFF
        m[0x0F] = (m[0x0A] - m[0x0B]) & 0xFF
        m[0x10] = (m[0x08] - m[0x0F]) & 0xFF
        m[0x11] = (m[0x10] ^ m[0x07]) & 0xFF
        m[0x12] = (m[0x0F] ^ 0xFF) & 0xFF
        m[0x13] = (m[0x03] ^ 0x10) & 0xFF
        m[0x14] = (m[0x04] - 0x32) & 0xFF
        m[0x15] = (m[0x05] + 0xED) & 0xFF
        m[0x16] = (m[0x06] ^ 0xF3) & 0xFF
        m[0x17] = (m[0x13] - m[0x0F]) & 0xFF
        m[0x18] = (m[0x15] + m[0x07]) & 0xFF
        m[0x19] = (0x21 - m[0x13]) & 0xFF
        m[0x1A] = (m[0x14] ^ m[0x17]) & 0xFF
        m[0x1B] = (m[0x16] + m[0x16]) & 0xFF
        m[0x1C] = (m[0x17] + 0x44) & 0xFF
        m[0x1D] = (m[0x03] + m[0x04]) & 0xFF
        m[0x1E] = (m[0x05] - m[0x16]) & 0xFF
        m[0x1F] = (m[0x1D] ^ m[0x13]) & 0xFF

        return bytes(m)

    def decrypt_video(self, data: bytearray) -> None:
        """Unmask a video payload in place.

        The mask resets to plaintext ^ video_mask2 after every block, which collapses
        against the running XOR of the ciphertext blocks:

            even block:  plaintext = running ^ video_mask2
            odd block:   plaintext = running

        stages/crack.py relies on the same identity.
        """
        if not is_masked(len(data)):
            return

        mask1 = np.frombuffer(self.video_mask1, dtype=np.uint8)
        mask2 = np.frombuffer(self.video_mask2, dtype=np.uint8)
        buf = np.frombuffer(data, dtype=np.uint8)
        rows = (len(data) - CIPHER_START) // BLOCK

        body = buf[CIPHER_START : CIPHER_START + rows * BLOCK].reshape(rows, BLOCK)
        running = np.bitwise_xor.accumulate(body, axis=0)
        running[0::2] ^= mask2
        body[:] = running

        # A partial last block continues the chain with the mask the last full block
        # left behind, which is its plaintext ^ video_mask2.
        tail = CIPHER_START + rows * BLOCK
        if tail < len(data):
            buf[tail:] ^= (running[-1] ^ mask2)[: len(data) - tail]

        # The head is unmasked last, with video_mask1 accumulated over the decrypted body
        # blocks that follow it.
        head = buf[MASK_START:CIPHER_START].reshape(-1, BLOCK)
        later = buf[CIPHER_START : CIPHER_START + HEAD_SIZE].reshape(-1, BLOCK)
        head ^= mask1 ^ np.bitwise_xor.accumulate(later, axis=0)

    def demux(
        self,
        output_path: Path,
        reporter: Reporter,
        file_paths: dict[str, list[Path]] | None = None,
    ) -> dict[str, list[Path]]:
        base_name = self.file_path.stem
        streams: dict[Path, BufferedWriter] = {}
        if file_paths is None:
            file_paths = {}
        known = {b"CRID", b"@SFV", b"@SFA", b"@CUE", b"@APP", b"@ALP", b"@SBT"}
        file_size = self.file_path.stat().st_size

        with (
            reporter.task("demux", total=file_size, unit="B") as task,
            ExitStack() as open_streams,
        ):

            def write_to(filename: str, kind: str, payload: bytes) -> None:
                path = output_path / filename
                if path not in streams:
                    streams[path] = open_streams.enter_context(open(path, "wb"))
                    file_paths.setdefault(kind, []).append(path)
                streams[path].write(payload)

            for chunks, (header, data) in enumerate(read_chunks(self.file_path), start=1):
                payload_type = header.data_type & 0x3
                if header.signature == b"@SFV" and payload_type == 0:
                    buffer = bytearray(data)
                    self.decrypt_video(buffer)
                    write_to(f"{base_name}.ivf", "ivf", buffer)
                elif header.signature == b"@SFA" and payload_type == 0:
                    write_to(f"{base_name}_{header.channel_no}.hca", "hca", data)
                elif header.signature not in known:
                    known.add(header.signature)  # warn once per signature
                    log.warning(f"Unknown signature {header.signature!r}")

                task.advance(header.data_size + 8)
                if chunks % 100 == 0:
                    reporter.checkpoint()

            task.set_completed(file_size)

        return file_paths
