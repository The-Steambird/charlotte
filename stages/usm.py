import struct

from contextlib import ExitStack, closing
from typing import TYPE_CHECKING, NamedTuple

import numpy as np

from Crypto.Cipher import AES

from utils.errors import CharlotteError
from utils.logger import log


if TYPE_CHECKING:
    from collections.abc import Generator
    from io import BufferedWriter
    from pathlib import Path

    from resources.keys import DecryptionKey
    from utils.reporter import Reporter


# A payload starts data_offset bytes past byte 8 of its chunk, and the header already covers
# the first 0x18 of those.
HEADER_SIZE = 32
MIN_DATA_OFFSET = 0x18

# A video payload under the old mask:
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
    frame_time: int

    @classmethod
    def from_bytes(cls, raw: bytes) -> ChunkHeader:
        return cls._make(struct.unpack(">4s I x B H B 2x B I 12x", raw))

    @property
    def size(self) -> int:
        return 8 + self.data_size

    @property
    def is_data(self) -> bool:
        return self.data_type & 0x3 == 0

    @property
    def is_header(self) -> bool:
        return self.data_type & 0x3 == 1


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
            # Checked before reading because read() allocates the declared size up front and
            # a corrupt one could ask for 4 GB. A short read would also end the walk quietly
            # and leave a truncated .ivf that looks whole.
            if payload_size > file_size - fp.tell():
                raise CharlotteError(f"Truncated USM chunk: {file_path.name}")

            payload = fp.read(payload_size)
            fp.seek(header.padding_size, 1)
            yield header, payload


# Strings (0xA) and data (0xB) stay raw bytes because nothing reads them.
UTF_TYPES = {
    0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
    6: "Q", 7: "q", 8: "f", 9: "d", 0xA: "4s", 0xB: "8s",
}  # fmt: skip
UTF_ZERO, UTF_CONSTANT, UTF_PER_ROW = 0x10, 0x30, 0x50


def read_utf(payload: bytes) -> dict:
    """The first row of an @UTF table, which is the only row a stream header has."""
    table = payload[8 : 8 + struct.unpack_from(">I", payload, 4)[0]]
    row_at, strings_at, _, _, columns = struct.unpack_from(">HIIIH", table, 2)

    row = {}
    at = 24
    for _ in range(columns):
        flags, name_at = struct.unpack_from(">BI", table, at)
        at += 5
        form = ">" + UTF_TYPES[flags & 0x0F]
        storage = flags & 0xF0
        value = None
        if storage == UTF_ZERO:
            value = 0
        elif storage == UTF_CONSTANT:
            value = struct.unpack_from(form, table, at)[0]
            at += struct.calcsize(form)
        elif storage == UTF_PER_ROW:
            value = struct.unpack_from(form, table, row_at)[0]
            row_at += struct.calcsize(form)
        name_at += strings_at
        row[table[name_at : table.index(b"\x00", name_at)].decode()] = value
    return row


def video_nonce(file_path: Path) -> int | None:
    """None means the file predates 7.1 and uses the old mask."""
    with closing(read_chunks(file_path)) as chunks:
        for header, payload in chunks:
            if header.signature == b"@SFV":
                if not header.is_header:
                    return None
                try:
                    return read_utf(payload).get("nonce")
                except (struct.error, KeyError, ValueError) as e:
                    raise CharlotteError(f"Corrupt video header: {file_path.name}") from e
    return None


class USM:
    def __init__(self, file_path: Path, key: DecryptionKey, nonce: int | None = None):
        self.file_path = file_path
        self.video_mask1 = self.build_mask(key.key1, key.key2)
        self.video_mask2 = bytes(b ^ 0xFF for b in self.video_mask1)
        self.aes_key = key.aes_key
        self.nonce = nonce

    @staticmethod
    def build_mask(key1: bytes, key2: bytes) -> bytes:
        m = bytearray(0x20)

        m[0x00] = key1[0]
        m[0x01] = key1[1]
        m[0x02] = key1[2]
        m[0x03] = (key1[3] - 0x34) & 0xFF
        m[0x04] = (key2[0] + 0xF9) & 0xFF
        m[0x05] = key2[1] ^ 0x13
        m[0x06] = (key2[2] + 0x61) & 0xFF
        m[0x07] = m[0x00] ^ 0xFF
        m[0x08] = (m[0x02] + m[0x01]) & 0xFF
        m[0x09] = (m[0x01] - m[0x07]) & 0xFF
        m[0x0A] = m[0x02] ^ 0xFF
        m[0x0B] = m[0x01] ^ 0xFF
        m[0x0C] = (m[0x0B] + m[0x09]) & 0xFF
        m[0x0D] = (m[0x08] - m[0x03]) & 0xFF
        m[0x0E] = m[0x0D] ^ 0xFF
        m[0x0F] = (m[0x0A] - m[0x0B]) & 0xFF
        m[0x10] = (m[0x08] - m[0x0F]) & 0xFF
        m[0x11] = m[0x10] ^ m[0x07]
        m[0x12] = m[0x0F] ^ 0xFF
        m[0x13] = m[0x03] ^ 0x10
        m[0x14] = (m[0x04] - 0x32) & 0xFF
        m[0x15] = (m[0x05] + 0xED) & 0xFF
        m[0x16] = m[0x06] ^ 0xF3
        m[0x17] = (m[0x13] - m[0x0F]) & 0xFF
        m[0x18] = (m[0x15] + m[0x07]) & 0xFF
        m[0x19] = (0x21 - m[0x13]) & 0xFF
        m[0x1A] = m[0x14] ^ m[0x17]
        m[0x1B] = (m[0x16] + m[0x16]) & 0xFF
        m[0x1C] = (m[0x17] + 0x44) & 0xFF
        m[0x1D] = (m[0x03] + m[0x04]) & 0xFF
        m[0x1E] = (m[0x05] - m[0x16]) & 0xFF
        m[0x1F] = m[0x1D] ^ m[0x13]

        return bytes(m)

    def decrypt_video(self, data: bytearray) -> None:
        """The mask resets to plaintext ^ video_mask2 after every block, which collapses
        against the running XOR of the ciphertext blocks. stages/crack.py relies on the same
        identity:

            even block:  plaintext = running ^ video_mask2
            odd block:   plaintext = running
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

    def decrypt_stream(self, data: bytearray, frame_time: int) -> None:
        iv = struct.pack(">QI4x", self.nonce, frame_time)
        cipher = AES.new(self.aes_key, AES.MODE_CTR, nonce=b"", initial_value=iv)
        data[MASK_START:] = cipher.decrypt(data[MASK_START:])

    def demux(self, output_path: Path, reporter: Reporter, created: list[Path]) -> list[Path]:
        """Returns the .hca files. Every file joins `created` as soon as it is opened, which
        lets the caller clean up after a failure part way through."""
        base_name = self.file_path.stem
        video = output_path / f"{base_name}.ivf"
        streams: dict[Path, BufferedWriter] = {}
        known = {b"CRID", b"@SFV", b"@SFA", b"@CUE", b"@APP", b"@ALP", b"@SBT"}
        file_size = self.file_path.stat().st_size

        with (
            reporter.task("demux", total=file_size, unit="B") as task,
            ExitStack() as open_streams,
        ):

            def write_to(path: Path, payload: bytes) -> None:
                if path not in streams:
                    streams[path] = open_streams.enter_context(open(path, "wb"))
                    created.append(path)
                streams[path].write(payload)

            for count, (header, data) in enumerate(read_chunks(self.file_path), start=1):
                if header.signature == b"@SFV" and header.is_data:
                    buffer = bytearray(data)
                    if self.nonce is None:
                        self.decrypt_video(buffer)
                    else:
                        self.decrypt_stream(buffer, header.frame_time)
                    write_to(video, buffer)
                elif header.signature == b"@SFA" and header.is_data:
                    write_to(output_path / f"{base_name}_{header.channel_no}.hca", data)
                elif header.signature not in known:
                    known.add(header.signature)  # warn once per signature
                    log.warning(f"Unknown signature {header.signature!r}")

                task.advance(header.size)
                if count % 100 == 0:
                    reporter.checkpoint()

            task.set_completed(file_size)

        return [path for path in streams if path.suffix == ".hca"]
