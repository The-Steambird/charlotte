import struct

from pathlib import Path

from utils.errors import CharlotteError
from utils.ffmpeg import AUDIO_CODECS, run_ffmpeg
from utils.logger import log


def crc16_table() -> tuple[int, ...]:
    crc16_poly = 0x8005
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ crc16_poly if crc & 0x8000 else crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


CRC16_TABLE = crc16_table()


def crc16(data: bytes | bytearray | memoryview) -> int:
    table = CRC16_TABLE
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ table[(crc >> 8) ^ byte]) & 0xFFFF
    return crc


def create_table56(key: int) -> bytearray:
    table = bytearray(0x10)
    mul = (key & 1) << 3 | 5
    add = key & 0xE | 1
    key >>= 4

    for i in range(0x10):
        key = (key * mul + add) & 0xF
        table[i] = key

    return table


def build_cipher_table(ciph_type: int, key1: bytes, key2: bytes) -> bytearray:
    table = bytearray(0x100)

    if ciph_type == 0:
        table[:] = range(0x100)

    elif ciph_type == 1:
        v = 0
        for i in range(0xFF):
            v = (v * 13 + 11) & 0xFF
            if v == 0 or v == 0xFF:
                v = (v * 13 + 11) & 0xFF
            table[i] = v
        table[0] = 0
        table[0xFF] = 0xFF

    elif ciph_type == 0x38:
        t1 = bytearray(8)
        k1 = struct.unpack("<I", key1)[0]
        k2 = struct.unpack("<I", key2)[0]

        if k1 == 0:
            k2 = (k2 - 1) & 0xFFFFFFFF
        k1 = (k1 - 1) & 0xFFFFFFFF

        for i in range(7):
            t1[i] = k1 & 0xFF
            k1 = ((k1 >> 8) | (k2 << 24)) & 0xFFFFFFFF
            k2 >>= 8

        t2 = bytearray(
            [
                t1[1],
                t1[1] ^ t1[6],
                t1[2] ^ t1[3],
                t1[2],
                t1[2] ^ t1[1],
                t1[3] ^ t1[4],
                t1[3],
                t1[3] ^ t1[2],
                t1[4] ^ t1[5],
                t1[4],
                t1[4] ^ t1[3],
                t1[5] ^ t1[6],
                t1[5],
                t1[5] ^ t1[4],
                t1[6] ^ t1[1],
                t1[6],
            ]
        )

        t3 = bytearray(0x100)
        t31 = create_table56(t1[0])

        for i in range(0x10):
            t32 = create_table56(t2[i])
            v = t31[i] << 4
            for j, val in enumerate(t32):
                t3[i * 0x10 + j] = v | val

        i_table = 1
        v = 0
        for _ in range(0x100):
            v = (v + 0x11) & 0xFF
            a = t3[v]
            if a != 0 and a != 0xFF:
                table[i_table] = a
                i_table += 1

        table[0] = 0
        table[0xFF] = 0xFF

    return table


class HCA:
    def __init__(self, file_path: Path, key1: bytes | None = None, key2: bytes | None = None):
        self.file_path = Path(file_path)
        self.key1 = key1 or bytes(4)
        self.key2 = key2 or bytes(4)
        self.block_count = 0
        self.block_size = 0
        self.ciph_type = 0
        self.ciph_offset = 0
        self.header = bytearray()
        self.data = bytearray()
        try:
            self.read_header()
        except struct.error as e:
            raise CharlotteError(f"Corrupt HCA header: {self.file_path.name}") from e

    def match_chunk(self, offset: int, tag: bytes) -> bool:
        sig = bytes(b & 0x7F for b in self.header[offset : offset + 4])
        if sig != tag:
            return False
        self.header[offset : offset + 4] = sig
        return True

    def read_header(self) -> None:
        blob = self.file_path.read_bytes()
        if len(blob) < 8:
            raise CharlotteError(f"Invalid HCA file: {self.file_path.name}")

        data_offset = struct.unpack(">H", blob[6:8])[0]
        self.header = bytearray(blob[:data_offset])

        if not self.match_chunk(0, b"HCA\x00"):
            raise CharlotteError(f"Invalid HCA header: {self.file_path.name}")
        offset = 8

        if not self.match_chunk(offset, b"fmt\x00"):
            raise CharlotteError(f"fmt chunk not found: {self.file_path.name}")
        self.block_count = struct.unpack(">I", self.header[offset + 8 : offset + 12])[0]
        offset += 16

        if self.match_chunk(offset, b"comp"):
            self.block_size = struct.unpack(">H", self.header[offset + 4 : offset + 6])[0]
            offset += 16
        elif self.match_chunk(offset, b"dec\x00"):
            self.block_size = struct.unpack(">H", self.header[offset + 4 : offset + 6])[0]
            offset += 12
        else:
            raise CharlotteError(f"comp/dec chunk not found: {self.file_path.name}")

        if self.block_size == 0:
            raise CharlotteError(f"HCA has no audio blocks: {self.file_path.name}")

        if self.match_chunk(offset, b"vbr\x00"):
            offset += 8
        if self.match_chunk(offset, b"ath\x00"):
            offset += 6
        if self.match_chunk(offset, b"loop"):
            offset += 16
        if self.match_chunk(offset, b"ciph"):
            self.ciph_type = struct.unpack(">H", self.header[offset + 4 : offset + 6])[0]
            if self.ciph_type not in (0, 1, 0x38):
                raise CharlotteError(f"Invalid cipher type: {self.ciph_type}")
            self.ciph_offset = offset
            offset += 6
        if self.match_chunk(offset, b"rva\x00"):
            offset += 8
        if self.match_chunk(offset, b"comm"):
            offset += 5
        if self.match_chunk(offset, b"pad\x00"):
            offset += 4

        crc = crc16(self.header[:-2])
        struct.pack_into(">H", self.header, len(self.header) - 2, crc)

        expected = self.block_size * self.block_count
        self.data = bytearray(blob[data_offset : data_offset + expected])
        if len(self.data) < expected:
            log.warning(
                f"{self.file_path.name} declares {self.block_count} audio blocks "
                f"but holds only {len(self.data) // self.block_size}."
            )

    def decrypt(self) -> None:
        if self.ciph_type == 0:
            return

        table = build_cipher_table(self.ciph_type, self.key1, self.key2)
        self.data = self.data.translate(table)

        self.ciph_type = 0
        struct.pack_into(">H", self.header, self.ciph_offset + 4, 0)
        crc = crc16(self.header[:-2])
        struct.pack_into(">H", self.header, len(self.header) - 2, crc)

    def save(self) -> None:
        size = self.block_size
        view = memoryview(self.data)
        for offset in range(0, len(self.data) - size + 1, size):
            crc = crc16(view[offset : offset + size - 2])
            struct.pack_into(">H", self.data, offset + size - 2, crc)

        with open(self.file_path, "wb") as f:
            f.write(self.header)
            f.write(self.data)

    def convert(self, output_file: Path, codec: str = "flac") -> None:
        args = ["-f", "hca", "-i", "pipe:0", *AUDIO_CODECS[codec][1], str(output_file)]
        run_ffmpeg(args, "Audio conversion failed", input=b"".join((self.header, self.data)))
