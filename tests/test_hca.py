"""The decryption itself is deliberately not tested, because a test would have to encrypt
with the same tables the line-for-line port decrypts with and would pass whether or not the
port matches the original. The corpus is the check that matters."""

import struct

import pytest

from conftest import flag_value
from stages.hca import HCA, crc16
from utils.errors import CharlotteError
from utils.ffmpeg import FFMPEG_MISSING


KEY1, KEY2 = bytes([0x11, 0x22, 0x33, 0x44]), bytes([0x55, 0x66, 0x77, 0x00])

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


def write_hca(tmp_path, blob: bytes, name: str = "Cs_Test_0.hca"):
    path = tmp_path / name
    path.write_bytes(blob)
    return path


def make_hca(tmp_path, name: str = "Cs_Test_0.hca") -> HCA:
    return HCA(write_hca(tmp_path, hca_bytes(), name), KEY1, KEY2)


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
    hca = HCA(path, KEY1, KEY2)

    assert hca.block_count == 3
    assert hca.block_size == 0x40
    assert hca.ciph_type == 0x38
    assert len(hca.data) == 0x40 * 3  # bounded by the declared blocks, not end of file


def test_short_file_warns_about_missing_blocks(tmp_path, caplog):
    path = write_hca(tmp_path, hca_bytes(block_size=0x20, block_count=4)[:-0x30])
    hca = HCA(path, KEY1, KEY2)

    assert len(hca.data) == 0x20 * 4 - 0x30
    assert "declares 4 audio blocks but holds only 2" in caplog.text


# --- header rejections ---


@pytest.mark.parametrize(
    "blob, match",
    [
        (b"HCA\x00", "Invalid HCA file"),
        (hca_bytes().replace(b"HCA\x00", b"XXXX", 1), "Invalid HCA header"),
        (hca_bytes().replace(b"fmt\x00", b"junk", 1), "fmt chunk not found"),
        (hca_bytes().replace(b"comp", b"junk", 1), "comp/dec chunk not found"),
    ],
    ids=["too-short", "bad-magic", "no-fmt", "no-comp"],
)
def test_malformed_header_rejected(tmp_path, blob, match):
    path = write_hca(tmp_path, blob)
    with pytest.raises(CharlotteError, match=match):
        HCA(path, KEY1, KEY2)


def test_zero_block_size_raises(tmp_path):
    """The C# allows it, but it is the stride save() walks by, and a bare ValueError there
    escapes the per-file handler and kills the whole batch."""
    path = write_hca(tmp_path, hca_bytes(block_size=0, block_count=0))
    with pytest.raises(CharlotteError, match="no audio blocks"):
        HCA(path, KEY1, KEY2)


def test_unknown_cipher_type_raises(tmp_path):
    """Anything but 0, 1 and 0x38 would build an all-zero table and translate the whole
    stream to zeros."""
    path = write_hca(tmp_path, hca_bytes(ciph_type=2))
    with pytest.raises(CharlotteError, match="Invalid cipher type"):
        HCA(path, KEY1, KEY2)


def test_truncated_header_raises(tmp_path):
    """data_offset past the end of the file raises struct.error, which is translated
    rather than leaked."""
    path = write_hca(tmp_path, hca_bytes()[:12])
    with pytest.raises(CharlotteError, match="Corrupt HCA header"):
        HCA(path, KEY1, KEY2)


# --- save ---


def test_save_overwrites_the_source_with_the_in_memory_stream(tmp_path):
    # Zeros pass through the cipher table untouched, and a zeroed body would pass on the
    # header edit alone.
    body = bytes(range(0x20)) * 4
    original = hca_bytes(ciph_type=0x38, block_count=4, data=body)
    path = write_hca(tmp_path, original)
    hca = HCA(path, KEY1, KEY2)
    hca.decrypt()

    hca.save()

    written = path.read_bytes()
    assert written == bytes(hca.header) + bytes(hca.data)
    assert written[len(hca.header) :] != body
    assert HCA(path, KEY1, KEY2).ciph_type == 0


# --- convert ---


def test_convert_pipes_the_stream_to_ffmpeg(ffmpeg, tmp_path):
    hca = make_hca(tmp_path)
    output = hca.convert(output_path=tmp_path, codec="flac")

    assert output == tmp_path / "Cs_Test_0.flac"
    assert flag_value(ffmpeg.cmd, "-f") == "hca"
    assert flag_value(ffmpeg.cmd, "-i") == "pipe:0"
    assert ffmpeg.cmd[-1] == str(output)
    assert ffmpeg.input == bytes(hca.header) + bytes(hca.data)


def test_convert_extension_and_args_follow_the_codec(ffmpeg, tmp_path):
    output = make_hca(tmp_path).convert(output_path=tmp_path, codec="opus")

    assert output == tmp_path / "Cs_Test_0.mka"
    assert flag_value(ffmpeg.cmd, "-c:a") == "libopus"


def test_convert_reports_ffmpeg_failure(ffmpeg, tmp_path, caplog):
    ffmpeg.returncode = 1
    ffmpeg.stderr = b"Invalid data found when processing input"

    with pytest.raises(CharlotteError, match="Audio conversion failed"):
        make_hca(tmp_path).convert(output_path=tmp_path)

    assert "Invalid data found" in caplog.text


def test_convert_without_ffmpeg_raises(ffmpeg, tmp_path):
    ffmpeg.missing = True
    with pytest.raises(CharlotteError) as excinfo:
        make_hca(tmp_path).convert(output_path=tmp_path)
    assert str(excinfo.value) == FFMPEG_MISSING
