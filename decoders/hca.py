import struct
import subprocess

from pathlib import Path

from utils.errors import CharlotteError
from utils.logger import log
from utils.paths import bundle_root


CRC16_TABLE = (
    0x0000, 0x8005, 0x800F, 0x000A, 0x801B, 0x001E, 0x0014, 0x8011, 0x8033, 0x0036, 0x003C,
    0x8039, 0x0028, 0x802D, 0x8027, 0x0022, 0x8063, 0x0066, 0x006C, 0x8069, 0x0078, 0x807D,
    0x8077, 0x0072, 0x0050, 0x8055, 0x805F, 0x005A, 0x804B, 0x004E, 0x0044, 0x8041, 0x80C3,
    0x00C6, 0x00CC, 0x80C9, 0x00D8, 0x80DD, 0x80D7, 0x00D2, 0x00F0, 0x80F5, 0x80FF, 0x00FA,
    0x80EB, 0x00EE, 0x00E4, 0x80E1, 0x00A0, 0x80A5, 0x80AF, 0x00AA, 0x80BB, 0x00BE, 0x00B4,
    0x80B1, 0x8093, 0x0096, 0x009C, 0x8099, 0x0088, 0x808D, 0x8087, 0x0082, 0x8183, 0x0186,
    0x018C, 0x8189, 0x0198, 0x819D, 0x8197, 0x0192, 0x01B0, 0x81B5, 0x81BF, 0x01BA, 0x81AB,
    0x01AE, 0x01A4, 0x81A1, 0x01E0, 0x81E5, 0x81EF, 0x01EA, 0x81FB, 0x01FE, 0x01F4, 0x81F1,
    0x81D3, 0x01D6, 0x01DC, 0x81D9, 0x01C8, 0x81CD, 0x81C7, 0x01C2, 0x0140, 0x8145, 0x814F,
    0x014A, 0x815B, 0x015E, 0x0154, 0x8151, 0x8173, 0x0176, 0x017C, 0x8179, 0x0168, 0x816D,
    0x8167, 0x0162, 0x8123, 0x0126, 0x012C, 0x8129, 0x0138, 0x813D, 0x8137, 0x0132, 0x0110,
    0x8115, 0x811F, 0x011A, 0x810B, 0x010E, 0x0104, 0x8101, 0x8303, 0x0306, 0x030C, 0x8309,
    0x0318, 0x831D, 0x8317, 0x0312, 0x0330, 0x8335, 0x833F, 0x033A, 0x832B, 0x032E, 0x0324,
    0x8321, 0x0360, 0x8365, 0x836F, 0x036A, 0x837B, 0x037E, 0x0374, 0x8371, 0x8353, 0x0356,
    0x035C, 0x8359, 0x0348, 0x834D, 0x8347, 0x0342, 0x03C0, 0x83C5, 0x83CF, 0x03CA, 0x83DB,
    0x03DE, 0x03D4, 0x83D1, 0x83F3, 0x03F6, 0x03FC, 0x83F9, 0x03E8, 0x83ED, 0x83E7, 0x03E2,
    0x83A3, 0x03A6, 0x03AC, 0x83A9, 0x03B8, 0x83BD, 0x83B7, 0x03B2, 0x0390, 0x8395, 0x839F,
    0x039A, 0x838B, 0x038E, 0x0384, 0x8381, 0x0280, 0x8285, 0x828F, 0x028A, 0x829B, 0x029E,
    0x0294, 0x8291, 0x82B3, 0x02B6, 0x02BC, 0x82B9, 0x02A8, 0x82AD, 0x82A7, 0x02A2, 0x82E3,
    0x02E6, 0x02EC, 0x82E9, 0x02F8, 0x82FD, 0x82F7, 0x02F2, 0x02D0, 0x82D5, 0x82DF, 0x02DA,
    0x82CB, 0x02CE, 0x02C4, 0x82C1, 0x8243, 0x0246, 0x024C, 0x8249, 0x0258, 0x825D, 0x8257,
    0x0252, 0x0270, 0x8275, 0x827F, 0x027A, 0x826B, 0x026E, 0x0264, 0x8261, 0x0220, 0x8225,
    0x822F, 0x022A, 0x823B, 0x023E, 0x0234, 0x8231, 0x8213, 0x0216, 0x021C, 0x8219, 0x0208,
    0x820D, 0x8207, 0x0202,
)  # fmt: skip


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


AUDIO_CODECS = {
    "flac": (".flac", ["-compression_level", "8"]),
    "opus": (".mka", ["-c:a", "libopus", "-b:a", "256k", "-vbr", "on"]),
}


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

        self.data = bytearray(blob[data_offset : data_offset + self.block_size * self.block_count])

    def decrypt(self) -> None:
        """Decrypt header and data in memory leaving file on disk untouched."""
        if self.ciph_type == 0:
            return

        table = build_cipher_table(self.ciph_type, self.key1, self.key2)
        size = self.block_size
        self.data = self.data.translate(table)
        view = memoryview(self.data)

        for offset in range(0, len(self.data) - size + 1, size):
            crc = crc16(view[offset : offset + size - 2])
            struct.pack_into(">H", self.data, offset + size - 2, crc)

        self.ciph_type = 0
        struct.pack_into(">H", self.header, self.ciph_offset + 4, 0)
        crc = crc16(self.header[:-2])
        struct.pack_into(">H", self.header, len(self.header) - 2, crc)

    def save(self) -> None:
        """Write the decrypted stream back to the source .hca for -nc runs."""
        with open(self.file_path, "wb") as f:
            f.write(self.header)
            f.write(self.data)

    def convert(self, output_path: Path, codec: str = "flac") -> Path:
        extension, codec_args = AUDIO_CODECS.get(codec, AUDIO_CODECS["flac"])
        output_file = output_path / f"{self.file_path.stem}{extension}"
        cmd = [
            str(bundle_root() / "ffmpeg.exe"),
            "-y",  # Overwrite output file
            "-loglevel", "error",
            "-f", "hca",
            "-i", "pipe:0",
            *codec_args,
            str(output_file),
        ]  # fmt: skip

        try:
            # Read from memory so the decrypted stream don't need to write to disk first to read.
            payload = b"".join((self.header, self.data))
            subprocess.run(cmd, input=payload, capture_output=True, check=True)
            return output_file
        except subprocess.CalledProcessError as e:
            log.error(f"Error converting audio: {e}")
            if e.stderr:
                log.error(e.stderr.decode("utf-8", errors="replace"))
            raise CharlotteError("Audio conversion failed.") from e
        except FileNotFoundError:
            log.error("FFmpeg not found. Place FFmpeg in the root directory and try again.")
            raise CharlotteError("FFmpeg not found.") from None
