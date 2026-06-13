import struct

from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from utils.errors import CharlotteError
from utils.logger import log


if TYPE_CHECKING:
    from io import BufferedWriter

    from utils.reporter import Reporter


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
        if len(data) - 0x40 < 0x200:
            return

        mask2 = int.from_bytes(self.video_mask2)
        end = len(data)

        m = mask2
        pos = 0x140
        while pos + 0x20 <= end:
            dec = int.from_bytes(data[pos : pos + 0x20]) ^ m
            data[pos : pos + 0x20] = dec.to_bytes(0x20)
            m = dec ^ mask2
            pos += 0x20
        if pos < end:
            tail = end - pos
            dec = int.from_bytes(data[pos:end]) ^ (m >> (8 * (0x20 - tail)))
            data[pos:end] = dec.to_bytes(tail)

        m = int.from_bytes(self.video_mask1)
        for pos in range(0x40, 0x140, 0x20):
            m ^= int.from_bytes(data[pos + 0x100 : pos + 0x100 + 0x20])
            dec = int.from_bytes(data[pos : pos + 0x20]) ^ m
            data[pos : pos + 0x20] = dec.to_bytes(0x20)

    def demux(self, output_path: Path, reporter: Reporter) -> dict[str, list[Path]]:
        base_name = self.file_path.stem
        streams: dict[Path, BufferedWriter] = {}
        file_paths: dict[str, list[Path]] = {}
        known = {b"CRID", b"@SFV", b"@SFA", b"@CUE", b"@APP", b"@ALP", b"@SBT"}
        file_size = self.file_path.stat().st_size

        with (
            open(self.file_path, "rb") as fp,
            reporter.task("demux", total=file_size, unit="B") as task,
            ExitStack() as open_streams,
        ):

            def write_to(filename: str, kind: str, payload: bytes) -> None:
                path = output_path / filename
                if path not in streams:
                    streams[path] = open_streams.enter_context(open(path, "wb"))
                    file_paths.setdefault(kind, []).append(path)
                streams[path].write(payload)

            while True:
                header_data = fp.read(32)
                if len(header_data) < 32:
                    task.advance(len(header_data))
                    break

                header = ChunkHeader.from_bytes(header_data)
                task.advance(header.data_size + 8)

                payload_size = header.data_size - header.data_offset - header.padding_size
                if payload_size < 0:
                    raise CharlotteError(f"Corrupt USM chunk in {self.file_path.name}")

                fp.seek(header.data_offset - 0x18, 1)
                data = fp.read(payload_size)
                fp.seek(header.padding_size, 1)

                payload_type = header.data_type & 0x3
                if header.signature == b"@SFV" and payload_type == 0:
                    buffer = bytearray(data)
                    self.decrypt_video(buffer)
                    write_to(f"{base_name}.ivf", "ivf", buffer)
                elif header.signature == b"@SFA" and payload_type == 0:
                    write_to(f"{base_name}_{header.channel_no}.hca", "hca", data)
                elif header.signature not in known:
                    log.warning(f"Unknown signature {header.signature!r}")

        return file_paths
