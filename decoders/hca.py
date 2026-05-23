import struct
import subprocess
import sys

from dataclasses import dataclass
from pathlib import Path

import typer

from utils.logger import log


@dataclass
class HCAHeader:
    """HCA file header information."""

    version: int = 0
    data_offset: int = 0
    block_count: int = 0
    block_size: int = 0
    ciph_type: int = 0


class HCA:
    """HCA audio decoder.

    Decrypts CRI Middleware's HCA (High Compression Audio) format and converts to FLAC via ffmpeg.

    Args:
        file_path: Path to the HCA file
        key1: Optional decryption key (4 bytes) for cipher type 56
        key2: Optional decryption key (4 bytes) for cipher type 56
    """

    def __init__(self, file_path: Path, key1: bytes | None = None, key2: bytes | None = None):
        self.file_path = Path(file_path)
        self.key1 = key1 or bytes(4)
        self.key2 = key2 or bytes(4)
        self.ciph_table = bytearray(0x100)
        self.encrypted = False
        self.header_struct = HCAHeader()
        self.header_bytes = bytearray()
        self.data = bytearray()
        self._read_header()

    def _init56_create_table(self, key: int) -> bytearray:
        """Generate a substitution table for cipher type 56.

        Args:
            key: Input key byte for table generation.

        Returns:
            A 16-byte substitution table.
        """
        table = bytearray(0x10)
        mul = (key & 1) << 3 | 5
        add = key & 0xE | 1
        key >>= 4

        for i in range(0x10):
            key = (key * mul + add) & 0xF
            table[i] = key

        return table

    def _init_mask(self, mask_type: int) -> None:
        """Initialize the cipher mask table based on the cipher type.

        Args:
            mask_type: The cipher type identifier (0 = none, 1 = constant, 56 = key-based).
        """
        if mask_type == 0:
            for i in range(0x100):
                self.ciph_table[i] = i

        elif mask_type == 1:
            v = 0
            for i in range(0xFF):
                v = (v * 13 + 11) & 0xFF
                if v == 0 or v == 0xFF:
                    v = (v * 13 + 11) & 0xFF
                self.ciph_table[i] = v
            self.ciph_table[0] = 0
            self.ciph_table[0xFF] = 0xFF

        elif mask_type == 56:
            t1 = bytearray(8)
            key1 = struct.unpack("<I", self.key1)[0]
            key2 = struct.unpack("<I", self.key2)[0]

            if key1 == 0:
                key2 = (key2 - 1) & 0xFFFFFFFF
            key1 = (key1 - 1) & 0xFFFFFFFF

            for i in range(7):
                t1[i] = key1 & 0xFF
                key1 = ((key1 >> 8) | (key2 << 24)) & 0xFFFFFFFF
                key2 >>= 8

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
            t31 = self._init56_create_table(t1[0])

            for i in range(0x10):
                t32 = self._init56_create_table(t2[i])
                v = t31[i] << 4
                for j, val in enumerate(t32):
                    t3[i * 0x10 + j] = v | val

            i_table = 1
            v = 0
            for _ in range(0x100):
                v = (v + 0x11) & 0xFF
                a = t3[v]
                if a != 0 and a != 0xFF:
                    self.ciph_table[i_table] = a
                    i_table += 1

            self.ciph_table[0] = 0
            self.ciph_table[0xFF] = 0xFF

    def _mask(self, data: bytearray) -> None:
        """Apply cipher mask to data."""
        data[:] = data.translate(self.ciph_table)

    @staticmethod
    def _checksum(data: bytes) -> int:
        """Calculate 16-bit checksum for HCA blocks.

        Args:
            data: The data bytes to checksum (excluding the checksum field itself).

        Returns:
            The calculated 16-bit checksum.
        """
        v = [
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
        ]  # fmt: skip

        checksum = 0
        for byte in data:
            checksum = ((checksum << 8) ^ v[(checksum >> 8) ^ byte]) & 0xFFFF
        return checksum

    def _read_header(self) -> None:
        """Read and parse the HCA file header structure.

        Parses magic bytes, version, header chunks (fmt, comp/dec, vbr, ath, loop, ciph, rva, comm, pad),
        and verifies the header integrity.
        """
        with open(self.file_path, "rb") as fp:
            # Read magic header
            hca_bytes = bytearray(fp.read(8))

            magic = 0xFFFFFFFF
            sign = struct.unpack("<I", hca_bytes[0:4])[0] & 0x7F7F7F7F
            if sign == 0x00414348:  # "HCA\x00"
                magic = 0x7F7F7F7F
                self.encrypted = True

            sign = struct.unpack("<I", hca_bytes[0:4])[0] & magic
            if sign != 0x00414348:
                raise ValueError("Invalid HCA header")

            # Write unmasked signature back to hca_bytes
            struct.pack_into("<I", hca_bytes, 0, sign)

            self.header_struct.version = struct.unpack(">H", hca_bytes[4:6])[0]
            self.header_struct.data_offset = struct.unpack(">H", hca_bytes[6:8])[0]

            # Read full header
            fp.seek(0)
            self.header_bytes = bytearray(fp.read(self.header_struct.data_offset))

            # Copy unmasked hca_bytes into header
            self.header_bytes[0:8] = hca_bytes[0:8]

            header_offset = 8

            # Parse fmt block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00746D66:  # "fmt\x00"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.block_count = struct.unpack(
                    ">I", self.header_bytes[header_offset + 8 : header_offset + 12]
                )[0]
                header_offset += 16
            else:
                raise ValueError("fmt block not found")

            # Parse comp or dec block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x706D6F63:  # "comp"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.block_size = struct.unpack(
                    ">H", self.header_bytes[header_offset + 4 : header_offset + 6]
                )[0]
                header_offset += 16

            elif sign == 0x00636564:  # "dec"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.block_size = struct.unpack(
                    ">H", self.header_bytes[header_offset + 4 : header_offset + 6]
                )[0]
                header_offset += 12
            else:
                raise ValueError("comp/dec block not found")

            # Parse optional vbr block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00726276:  # "vbr"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 8

            # Parse optional ath block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00687461:  # "ath"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 6

            # Parse optional loop block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x706F6F6C:  # "loop"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 16

            # Parse optional ciph block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x68706963:  # "ciph"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.ciph_type = struct.unpack(
                    ">H", self.header_bytes[header_offset + 4 : header_offset + 6]
                )[0]
                if self.header_struct.ciph_type not in (0, 1, 0x38):
                    raise ValueError(f"Invalid cipher type: {self.header_struct.ciph_type}")
                header_offset += 6

            # Parse optional rva block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00617672:  # "rva"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 8

            # Parse optional comm block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x6D6D6F63:  # "comm"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 5

            # Parse optional pad block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00646170:  # "pad"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 4

            # Update checksum
            checksum = self._checksum(self.header_bytes[:-2])
            struct.pack_into(">H", self.header_bytes, len(self.header_bytes) - 2, checksum)

            # Read audio data
            self.data = bytearray(
                fp.read(self.header_struct.block_size * self.header_struct.block_count)
            )

        self._init_mask(self.header_struct.ciph_type)

    def decrypt(self) -> None:
        """Decrypt the audio data in-place and save to disk.

        This method applies the initialized cipher mask to each block of the audio data,
        updates the block checksums, and overwrites the input file with the decrypted version.
        If the file is not encrypted (ciph_type == 0), this operation does nothing.
        """
        if self.header_struct.ciph_type == 0:
            return

        for i in range(self.header_struct.block_count):
            offset = i * self.header_struct.block_size
            block = bytearray(self.data[offset : offset + self.header_struct.block_size])
            self._mask(block)

            # Update checksum
            checksum = self._checksum(block[:-2])
            struct.pack_into(">H", block, len(block) - 2, checksum)
            self.data[offset : offset + self.header_struct.block_size] = block

        # Update header to reflect decryption
        self.header_struct.ciph_type = 0
        try:
            ciph_start = self.header_bytes.index(b"ciph")
            struct.pack_into(">H", self.header_bytes, ciph_start + 4, 0)
        except ValueError:
            pass

        # Re-checksum the header
        checksum = self._checksum(self.header_bytes[:-2])
        struct.pack_into(">H", self.header_bytes, len(self.header_bytes) - 2, checksum)

        # Write decrypted HCA back to file
        with open(self.file_path, "wb") as f:
            f.write(self.header_bytes)
            f.write(self.data)

    def convert_to_flac(self, output_path: Path) -> Path:
        """Convert HCA to FLAC using ffmpeg."""
        flac_file = output_path / f"{self.file_path.stem}.flac"
        root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent.parent
        # Build ffmpeg command - use the already decrypted HCA file
        cmd = [
            str(root / "ffmpeg.exe"),
            "-y",  # Overwrite output file
            "-loglevel",
            "error",  # Only show errors
            "-i",
            str(self.file_path),
            "-compression_level",
            "8",
            str(flac_file),
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return flac_file
        except subprocess.CalledProcessError as e:
            log.error(f"Error converting audio: {e}")
            if e.stderr:
                log.error(f"{e.stderr}")
            raise typer.Exit(1) from e
        except FileNotFoundError:
            log.error("ffmpeg not found. Place ffmpeg in the root directory and try again.")
            raise typer.Exit(1) from None
