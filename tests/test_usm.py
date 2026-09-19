import random
import struct

import pytest

from conftest import CancellingReporter, chunk
from stages.usm import MASK_START, MIN_MASKED, USM
from utils.errors import Cancelled, CharlotteError


def make_usm(tmp_path, chunks: bytes) -> USM:
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(chunks)
    return USM(usm_file, bytes(4), bytes(4))


def mask_video_reference(usm: USM, data: bytearray) -> None:
    """GICutscenes USM.cs::MaskVideo, transcribed byte for byte. decrypt_video collapses
    this into whole-block XORs; checking against the original rather than a previous
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


# The threshold, then every shape of trailing partial block at both block-count
# parities, since an odd count leaves a different mask behind for the tail.
THRESHOLD = MASK_START + MIN_MASKED


@pytest.mark.parametrize(
    "size",
    [THRESHOLD, THRESHOLD + 1, THRESHOLD + 0x1F, THRESHOLD + 0x20, THRESHOLD + 0x21, 0x4321],
)
def test_decrypt_video_matches_the_reference_chain(tmp_path, size):
    rng = random.Random(size)
    payload = bytes(rng.randrange(256) for _ in range(size))
    key1, key2 = bytes([0x11, 0x22, 0x33, 0x44]), bytes([0x55, 0x66, 0x77, 0x00])
    usm = USM(tmp_path / "Cs_Test.usm", key1, key2)

    blocks, reference = bytearray(payload), bytearray(payload)
    usm.decrypt_video(blocks)
    mask_video_reference(usm, reference)

    assert blocks != payload  # a no-op would satisfy the comparison but not the caller
    assert blocks == reference


def test_demux_extracts_streams(tmp_path, out_dir, reporter):
    # The video chunk is below the masking threshold, so it comes out untouched.
    data = (
        chunk(b"@SFV", b"video")
        + chunk(b"@SFA", b"audio0", channel=0)
        + chunk(b"@SFA", b"audio1", channel=1)
    )
    file_paths = make_usm(tmp_path, data).demux(out_dir, reporter)

    assert (out_dir / "Cs_Test.ivf").read_bytes() == b"video"
    assert (out_dir / "Cs_Test_0.hca").read_bytes() == b"audio0"
    assert (out_dir / "Cs_Test_1.hca").read_bytes() == b"audio1"
    assert [path.name for path in file_paths["ivf"]] == ["Cs_Test.ivf"]
    assert len(file_paths["hca"]) == 2


def test_metadata_chunks_skipped(tmp_path, out_dir, reporter):
    data = chunk(b"@SFV", b"meta", data_type=1) + chunk(b"@SFV", b"video")
    make_usm(tmp_path, data).demux(out_dir, reporter)
    assert (out_dir / "Cs_Test.ivf").read_bytes() == b"video"


def test_unknown_signature_warned_once(tmp_path, out_dir, reporter, caplog):
    data = chunk(b"@XXX", b"a") + chunk(b"@XXX", b"b") + chunk(b"@XXX", b"c")
    make_usm(tmp_path, data).demux(out_dir, reporter)
    warnings = [record for record in caplog.records if "Unknown signature" in record.message]
    assert len(warnings) == 1


def test_known_metadata_signatures_skipped_silently(tmp_path, out_dir, reporter, caplog):
    data = (
        chunk(b"CRID", b"header")
        + chunk(b"@CUE", b"cue")
        + chunk(b"@APP", b"app")
        + chunk(b"@SFV", b"video")
    )
    file_paths = make_usm(tmp_path, data).demux(out_dir, reporter)

    assert [record for record in caplog.records if "Unknown signature" in record.message] == []
    assert set(file_paths) == {"ivf"}  # only the video became an output stream


def test_corrupt_chunk_raises(tmp_path, out_dir, reporter):
    bad = struct.pack(">4sIxBHB2xB16x", b"@SFA", 4, 0x18, 0, 0, 0)  # data_size < data_offset
    with pytest.raises(CharlotteError, match="Corrupt USM chunk"):
        make_usm(tmp_path, bad).demux(out_dir, reporter)


def test_truncated_chunk_raises(tmp_path, out_dir, reporter):
    data = chunk(b"@SFV", b"video") + chunk(b"@SFA", b"audio")[:-2]
    with pytest.raises(CharlotteError, match="Truncated USM chunk"):
        make_usm(tmp_path, data).demux(out_dir, reporter)


def test_oversized_data_size_raises_before_reading(tmp_path, out_dir, reporter):
    """read() allocates the declared size up front, and MemoryError is not a CharlotteError."""
    bad = struct.pack(">4sIxBHB2xB16x", b"@SFA", 0xFFFFFFFF, 0x18, 0, 0, 0)
    with pytest.raises(CharlotteError, match="Truncated USM chunk"):
        make_usm(tmp_path, bad).demux(out_dir, reporter)


def test_undersized_data_offset_raises(tmp_path, out_dir, reporter):
    """Below 0x18 the walk would seek back into the header just read and creep."""
    bad = struct.pack(">4sIxBHB2xB16x", b"@SFA", 0, 0, 0, 0, 0)  # data_offset 0
    with pytest.raises(CharlotteError, match="Corrupt USM chunk"):
        make_usm(tmp_path, bad).demux(out_dir, reporter)


def test_cancel_mid_demux_records_partial_output(tmp_path, out_dir):
    """demux deletes nothing itself; the caller-supplied dict is how pipeline cleans up."""
    data = b"".join(chunk(b"@SFA", b"x") for _ in range(150))  # past the 100-chunk checkpoint
    file_paths = {}
    with pytest.raises(Cancelled):
        make_usm(tmp_path, data).demux(out_dir, CancellingReporter(), file_paths=file_paths)
    assert [path.name for path in file_paths["hca"]] == ["Cs_Test_0.hca"]
    assert (out_dir / "Cs_Test_0.hca").exists()
