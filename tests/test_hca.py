"""The cipher table is checked against a transcription of the C# rather than by a round trip,
because a round trip would encrypt with the same tables the port decrypts with and pass
whether or not they match the original."""

import random
import struct

import pytest

from conftest import flag_value
from resources.keys import DecryptionKey
from stages.hca import HCA, build_cipher_table, crc16
from utils.errors import CharlotteError


KEY = DecryptionKey(bytes([0x11, 0x22, 0x33, 0x44]), bytes([0x55, 0x66, 0x77, 0x00]))

# Chunks carry no length of their own, and these are the strides read_header walks by.
CHUNK_SIZES = {b"fmt\x00": 16, b"comp": 16, b"ciph": 6}


def hca_bytes(
    *,
    ciph_type: int = 0,
    block_size: int = 0x20,
    block_count: int = 2,
    data: bytes | None = None,
) -> bytes:
    chunks: list[tuple[bytes, dict]] = [
        (b"fmt\x00", {8: (">I", block_count)}),
        (b"comp", {4: (">H", block_size)}),
        (b"ciph", {4: (">H", ciph_type)}),
    ]

    header = bytearray(8)
    header[0:4] = b"HCA\x00"
    struct.pack_into(">H", header, 4, 0x0200)
    for tag, fields in chunks:
        base = len(header)
        header += bytearray(CHUNK_SIZES[tag])
        header[base : base + 4] = tag
        for offset, (fmt, value) in fields.items():
            struct.pack_into(fmt, header, base + offset, value)

    header += bytearray(2)  # checksum slot
    struct.pack_into(">H", header, 6, len(header))  # data_offset

    body = bytes(block_size * block_count) if data is None else data
    return bytes(header) + bytes(body)


def write_hca(tmp_path, blob: bytes):
    path = tmp_path / "Cs_Test_0.hca"
    path.write_bytes(blob)
    return path


def make_hca(tmp_path) -> HCA:
    return HCA(write_hca(tmp_path, hca_bytes()), KEY)


def cipher_table_reference(key1: bytes, key2: bytes) -> bytes:
    """GICutscenes HCA.cs::InitMask case 56, transcribed line for line. build_cipher_table
    takes t1 from the whole key at once, and the C# borrow into key2 is where the two could
    differ."""

    def create_table(key: int) -> list[int]:
        table = []
        mul = (key & 1) << 3 | 5
        add = key & 0xE | 1
        key >>= 4
        for _ in range(0x10):
            key = (key * mul + add) & 0xF
            table.append(key)
        return table

    k1 = int.from_bytes(key1, "little")
    k2 = int.from_bytes(key2, "little")
    if k1 == 0:
        k2 = (k2 - 1) & 0xFFFFFFFF
    k1 = (k1 - 1) & 0xFFFFFFFF
    t1 = []
    for _ in range(7):
        t1.append(k1 & 0xFF)
        k1 = (k1 >> 8 | k2 << 24) & 0xFFFFFFFF
        k2 >>= 8

    t2 = [
        t1[1], t1[1] ^ t1[6], t1[2] ^ t1[3], t1[2],
        t1[2] ^ t1[1], t1[3] ^ t1[4], t1[3], t1[3] ^ t1[2],
        t1[4] ^ t1[5], t1[4], t1[4] ^ t1[3], t1[5] ^ t1[6],
        t1[5], t1[5] ^ t1[4], t1[6] ^ t1[1], t1[6],
    ]  # fmt: skip

    t3 = [0] * 0x100
    t31 = create_table(t1[0])
    for i in range(0x10):
        v = (t31[i] << 4) & 0xFF
        for index, j in enumerate(create_table(t2[i])):
            t3[i * 0x10 + index] = v | j

    table = [0] * 0x100
    i_table = 1
    v = 0
    for _ in range(0x100):
        v = (v + 0x11) & 0xFF
        a = t3[v]
        if a not in (0, 0xFF):
            table[i_table] = a
            i_table += 1
    table[0] = 0
    table[0xFF] = 0xFF
    return bytes(table)


def random_keys(count: int) -> list[tuple[bytes, bytes]]:
    rng = random.Random(0)
    return [(rng.randbytes(4), rng.randbytes(4)) for _ in range(count)]


# --- cipher table ---


@pytest.mark.parametrize(
    "key1, key2",
    [
        *random_keys(10),
        pytest.param(bytes(4), bytes([0x55, 0x66, 0x77, 0]), id="borrow-from-key2"),
        pytest.param(bytes(4), bytes([0, 0, 0, 1]), id="wrapped-zero-key"),
    ],
)
def test_cipher_table_matches_the_reference(key1, key2):
    assert build_cipher_table(key1, key2) == cipher_table_reference(key1, key2)


# --- checksum ---


def test_crc16_check_value():
    """The table is generated from its polynomial, and the standard check value for CRC-16
    poly 0x8005 (unreflected, zero init) pins it to the one the C# shipped."""
    assert crc16(b"123456789") == 0xFEE8


# --- header walk ---


def test_header_fields_parsed(tmp_path):
    """This is the positive control for the rejections below, because a builder producing
    nonsense would still make all of them pass."""
    path = write_hca(tmp_path, hca_bytes(ciph_type=0x38, block_size=0x40, block_count=3))
    hca = HCA(path, KEY)

    assert hca.block_count == 3
    assert hca.block_size == 0x40
    assert hca.ciph_type == 0x38
    assert len(hca.data) == 0x40 * 3  # bounded by the declared blocks, not end of file


# --- header rejections ---


def test_truncated_header_raises(tmp_path):
    """A bare struct.error would escape the per-file handler and end the whole batch."""
    path = write_hca(tmp_path, hca_bytes()[:12])
    with pytest.raises(CharlotteError, match="Corrupt HCA header"):
        HCA(path, KEY)


def test_zero_block_size_raises(tmp_path):
    """The C# allows it, but save() divides the stream by it, and a bare ZeroDivisionError
    there escapes the per-file handler and kills the whole batch."""
    path = write_hca(tmp_path, hca_bytes(block_size=0, block_count=0))
    with pytest.raises(CharlotteError, match="no audio blocks"):
        HCA(path, KEY)


def test_unknown_cipher_type_raises(tmp_path):
    """Anything but 0 and 0x38 would go through the 0x38 table and come out as noise that
    ffmpeg decodes without an error. That includes the keyless type 1, which no stream uses."""
    path = write_hca(tmp_path, hca_bytes(ciph_type=1))
    with pytest.raises(CharlotteError, match="Invalid cipher type"):
        HCA(path, KEY)


# --- save ---


def test_save_overwrites_the_source_with_the_in_memory_stream(tmp_path):
    # Zeros pass through the cipher table untouched, and a zeroed body would pass on the
    # header edit alone.
    body = bytes(range(0x20)) * 4
    original = hca_bytes(ciph_type=0x38, block_count=4, data=body)
    path = write_hca(tmp_path, original)
    hca = HCA(path, KEY)
    hca.decrypt()

    hca.save()

    written = path.read_bytes()
    assert written == bytes(hca.header) + bytes(hca.data)
    assert written[len(hca.header) :] != body
    assert HCA(path, KEY).ciph_type == 0


def test_save_writes_each_block_crc(tmp_path):
    path = write_hca(tmp_path, hca_bytes(block_count=4, data=bytes(range(0x80))))
    hca = HCA(path, KEY)

    hca.save()

    data = path.read_bytes()[len(hca.header) :]
    blocks = [data[offset : offset + 0x20] for offset in range(0, len(data), 0x20)]
    assert len(blocks) == 4
    for block in blocks:
        assert int.from_bytes(block[-2:]) == crc16(block[:-2])


# --- convert ---


def test_convert_pipes_the_stream_to_ffmpeg(ffmpeg, tmp_path):
    hca = make_hca(tmp_path)
    output = tmp_path / "Cs_Test_0.flac"
    hca.convert(output, codec="flac")

    assert flag_value(ffmpeg.cmd, "-f") == "hca"
    assert flag_value(ffmpeg.cmd, "-i") == "pipe:0"
    assert ffmpeg.cmd[-1] == str(output)
    assert ffmpeg.input == bytes(hca.header) + bytes(hca.data)


def test_convert_reports_ffmpeg_failure(ffmpeg, tmp_path, caplog):
    ffmpeg.returncode = 1
    ffmpeg.stderr = b"Invalid data found when processing input"

    with pytest.raises(CharlotteError, match="Audio conversion failed"):
        make_hca(tmp_path).convert(tmp_path / "Cs_Test_0.flac", codec="flac")

    assert "Invalid data found" in caplog.text
