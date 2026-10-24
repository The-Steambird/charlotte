import random
import struct

import pytest

from Crypto.Cipher import AES

from conftest import chunk, video_header
from resources.keys import DecryptionKey
from stages.usm import MASK_START, MIN_MASKED, USM, decrypt_stream, video_nonce
from utils.errors import CharlotteError


def make_usm(tmp_path, chunks: bytes) -> USM:
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(chunks)
    return USM(usm_file, DecryptionKey(bytes(4), bytes(4)))


def mask_video_reference(usm: USM, data: bytearray) -> None:
    """GICutscenes USM.cs::MaskVideo, transcribed byte for byte. decrypt_video collapses
    this into whole-block XORs, and checking against the original rather than a previous
    version of the port is what keeps the two from drifting as a pair."""
    offset = MASK_START
    size = len(data) - offset
    if size < MIN_MASKED:
        return

    mask = bytearray(usm.video_mask2)
    for i in range(0x100, size):
        data[i + offset] ^= mask[i & 0x1F]
        mask[i & 0x1F] = data[i + offset] ^ usm.video_mask2[i & 0x1F]

    mask = bytearray(usm.video_mask1)
    for i in range(0x100):
        mask[i & 0x1F] ^= data[0x100 + i + offset]
        data[i + offset] ^= mask[i & 0x1F]


# --- decrypt_video ---


# The sizes cover the threshold and every shape of trailing partial block at both block-count
# parities, because an odd count leaves a different mask behind for the tail.
THRESHOLD = MASK_START + MIN_MASKED


@pytest.mark.parametrize(
    "size",
    [THRESHOLD, THRESHOLD + 1, THRESHOLD + 0x1F, THRESHOLD + 0x20, THRESHOLD + 0x21, 0x4321],
)
def test_decrypt_video_matches_the_reference_chain(tmp_path, size):
    rng = random.Random(size)
    payload = bytes(rng.randrange(256) for _ in range(size))
    key1, key2 = bytes([0x11, 0x22, 0x33, 0x44]), bytes([0x55, 0x66, 0x77, 0x00])
    usm = USM(tmp_path / "Cs_Test.usm", DecryptionKey(key1, key2))

    blocks, reference = bytearray(payload), bytearray(payload)
    usm.decrypt_video(blocks)
    mask_video_reference(usm, reference)

    assert blocks != payload  # a no-op would satisfy the comparison but not the caller
    assert blocks == reference


def test_decrypt_stream_counts_from_the_chunk_iv():
    """Checked against AES-ECB of the counter blocks rather than a CTR cipher, because a CTR
    cipher would share any mistake in how the IV is laid out."""
    key = bytes(range(16))
    data = bytearray(MASK_START + 0x20)

    decrypt_stream(data, key, nonce=0x0102030405060708, frame_time=0x0A0B0C0D)

    ecb = AES.new(key, AES.MODE_ECB)
    iv = bytes.fromhex("0102030405060708 0a0b0c0d 00000000")
    assert data[:MASK_START] == bytes(MASK_START)
    assert data[MASK_START:] == ecb.encrypt(iv) + ecb.encrypt(iv[:-1] + b"\x01")


@pytest.mark.parametrize(
    "chunks, nonce",
    [
        (video_header(nonce=0x3CEFE9EAB8C72E7A) + chunk(b"@SFV", b"video"), 0x3CEFE9EAB8C72E7A),
        (video_header() + chunk(b"@SFV", b"video"), None),
    ],
)
def test_video_nonce_comes_from_the_leading_video_header(tmp_path, chunks, nonce):
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(chunks)
    assert video_nonce(usm_file) == nonce


def test_demux_extracts_streams(tmp_path, out_dir, reporter):
    # The video chunk is below the masking threshold and comes out untouched.
    data = (
        chunk(b"@SFV", b"video")
        + chunk(b"@SFA", b"audio0", channel=0)
        + chunk(b"@SFA", b"audio1", channel=1)
    )
    created = []
    video, audio = make_usm(tmp_path, data).demux(out_dir, reporter, created)

    assert video == out_dir / "Cs_Test.ivf"
    assert video.read_bytes() == b"video"
    assert (out_dir / "Cs_Test_0.hca").read_bytes() == b"audio0"
    assert (out_dir / "Cs_Test_1.hca").read_bytes() == b"audio1"
    assert [path.name for path in audio] == ["Cs_Test_0.hca", "Cs_Test_1.hca"]
    assert [path.name for path in created] == ["Cs_Test.ivf", "Cs_Test_0.hca", "Cs_Test_1.hca"]


def test_metadata_chunks_skipped(tmp_path, out_dir, reporter):
    data = chunk(b"@SFV", b"meta", data_type=1) + chunk(b"@SFV", b"video")
    make_usm(tmp_path, data).demux(out_dir, reporter, [])
    assert (out_dir / "Cs_Test.ivf").read_bytes() == b"video"


def test_only_unknown_signatures_warn_and_only_once(tmp_path, out_dir, reporter, caplog):
    data = (
        chunk(b"CRID", b"header")
        + chunk(b"@CUE", b"cue")
        + chunk(b"@ALP", b"alpha")
        + chunk(b"@XXX", b"a")
        + chunk(b"@XXX", b"b")
        + chunk(b"@SFV", b"video")
    )
    created = []
    _, audio = make_usm(tmp_path, data).demux(out_dir, reporter, created)

    warnings = [record for record in caplog.records if "Unknown signature" in record.message]
    assert len(warnings) == 1
    assert audio == []
    assert [path.name for path in created] == ["Cs_Test.ivf"]


def bare_header(data_size: int, data_offset: int) -> bytes:
    return struct.pack(">4sIxBHB2xB16x", b"@SFA", data_size, data_offset, 0, 0, 0)


# The reasons are in CLAUDE.md under read_chunks().
@pytest.mark.parametrize(
    "data, match",
    [
        (bare_header(4, 0x18), "Corrupt USM chunk"),
        (bare_header(0, 0), "Corrupt USM chunk"),
        (chunk(b"@SFV", b"video") + chunk(b"@SFA", b"audio")[:-2], "Truncated USM chunk"),
        (bare_header(0xFFFFFFFF, 0x18), "Truncated USM chunk"),
    ],
    ids=["size-below-offset", "offset-inside-header", "cut-off-payload", "4gb-declared"],
)
def test_broken_chunk_rejected(tmp_path, out_dir, reporter, data, match):
    with pytest.raises(CharlotteError, match=match):
        make_usm(tmp_path, data).demux(out_dir, reporter, [])
