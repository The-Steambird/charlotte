import random

import numpy as np
import pytest

from conftest import chunk
from stages.crack import (
    SAMPLE_STEPS,
    STAGES,
    Recovery,
    Sample,
    Stats,
    collect,
    crack_key,
    evaluate,
    expand_0,
    expand_1_2,
    expand_3,
    expand_4,
    expand_5,
    expand_6,
    solve,
    split_key,
)
from stages.usm import BLOCK, CIPHER_START, MASK_START, MIN_MASKED, USM, is_masked


KEY1, KEY2 = bytes([0x11, 0x22, 0x33, 0x44]), bytes([0x55, 0x66, 0x77, 0x00])

# The value each stage feeds its expansion, read back out of a known-good mask.
SEEDS = {
    expand_1_2: lambda m: (m[0x01] << 8) | m[0x02],
    expand_0: lambda m: m[0x00],
    expand_3: lambda m: m[0x03],
    expand_4: lambda m: m[0x04],
    expand_5: lambda m: m[0x05],
    expand_6: lambda m: m[0x06],
}


def key_pairs(count: int) -> list[tuple[bytes, bytes]]:
    rng = random.Random(0)
    # build_mask never reads byte 3 of key2, which is why it always comes back as 0.
    return [
        (
            bytes(rng.randrange(256) for _ in range(4)),
            bytes(rng.randrange(256) for _ in range(3)) + b"\x00",
        )
        for _ in range(count)
    ]


def video_plaintext(rng, blocks: int) -> bytes:
    """Compressed VP9 is close to uniform except for 00,00 and FF,FF byte pairs running
    above chance, which is the only signal the solver has."""
    data = bytearray(rng.randrange(256) for _ in range(blocks * BLOCK))
    for _ in range(len(data) // 40):
        i = rng.randrange(len(data) - 1)
        data[i] = data[i + 1] = 0x00 if rng.random() < 0.7 else 0xFF
    return bytes(data)


def encrypt(plain: bytes, key1: bytes, key2: bytes) -> bytes:
    """Inverse of the chained region of USM.decrypt_video. The running mask starts at
    video_mask2 and resets to plaintext ^ video_mask2 after every block."""
    mask2 = bytes(b ^ 0xFF for b in USM.build_mask(key1, key2))
    payload = bytearray(CIPHER_START)  # the head is masked separately and stays blank here
    m = mask2
    for i in range(0, len(plain), BLOCK):
        block = plain[i : i + BLOCK]
        payload += bytes(b ^ k for b, k in zip(block, m, strict=True))
        m = bytes(b ^ k for b, k in zip(block, mask2, strict=True))
    return bytes(payload)


def video_chunk(rng, blocks: int = 900) -> bytes:
    return chunk(b"@SFV", b"DKIF" + encrypt(video_plaintext(rng, blocks), KEY1, KEY2)[4:])


def test_stages_cover_every_mask_entry_exactly_once():
    """A stage that forgets an entry silently drops it from scoring."""
    covered = [entry for stage in STAGES for entry in stage.entries]
    assert sorted(covered) == list(range(BLOCK))


def test_pair_schedule_covers_every_adjacent_pair_exactly_once():
    scored = [pair for stage in STAGES for pair in stage.pairs]
    assert sorted(scored) == list(range(BLOCK - 1))


@pytest.mark.parametrize(("key1", "key2"), key_pairs(12))
def test_expansions_reproduce_build_mask(key1, key2):
    truth = USM.build_mask(key1, key2)

    mask = np.zeros((BLOCK, 1), dtype=np.int32)
    for stage in STAGES:
        stage.expand(mask, np.array([SEEDS[stage.expand](truth)], dtype=np.int32))

    assert bytes(mask[:, 0].tolist()) == truth


@pytest.mark.parametrize(("key1", "key2"), key_pairs(12))
def test_split_key_inverts_build_mask(key1, key2):
    assert split_key(list(USM.build_mask(key1, key2))) == (key1, key2)


def test_solve_recovers_a_known_key():
    rng = random.Random(7)
    stats = Stats()
    for _ in range(8):
        stats.add(encrypt(video_plaintext(rng, 500), KEY1, KEY2))

    assert split_key(solve(*stats.tables())) == (KEY1, KEY2)


def test_crack_key_recovers_from_a_usm(tmp_path, reporter):
    rng = random.Random(3)
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(b"".join(video_chunk(rng) for _ in range(8)))

    assert crack_key(usm_file, reporter) == Recovery((KEY1, KEY2), "")


def test_crack_key_declines_a_file_with_no_video(tmp_path, reporter):
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(chunk(b"@SFA", b"audio"))

    recovery = crack_key(usm_file, reporter)

    assert recovery.key is None
    assert "no IVF video stream" in recovery.reason


def test_crack_key_declines_too_little_video(tmp_path, reporter):
    payload = b"DKIF" + bytes(MASK_START + MIN_MASKED)
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(b"".join(chunk(b"@SFV", payload) for _ in range(4)))

    recovery = crack_key(usm_file, reporter)

    assert recovery.key is None
    assert "bytes of encrypted video" in recovery.reason


def test_repeated_payloads_are_dealt_once(tmp_path, reporter):
    """A repeat must neither count toward the sample floor nor reach the second pool,
    where it would be the first pool's evidence counted twice."""
    payload = b"DKIF" + bytes(CIPHER_START + 100 * BLOCK - 4)
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(b"".join(chunk(b"@SFV", payload) for _ in range(5)))

    sample = collect(usm_file, reporter, SAMPLE_STEPS[0])

    assert sample.used == 100 * BLOCK
    assert (sample.left.blocks, sample.right.blocks) == (100, 0)


def test_crack_key_declines_a_repeated_payload(tmp_path, reporter):
    """The frame repeats past the sample floor, but split-half proves nothing when both
    pools would see the same bytes, which is why a single distinct payload is declined."""
    payload = b"DKIF" + bytes(CIPHER_START + 3200 * BLOCK - 4)
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(b"".join(chunk(b"@SFV", payload) for _ in range(4)))

    recovery = crack_key(usm_file, reporter)

    assert recovery.key is None
    assert "only one distinct video payload" in recovery.reason


def test_split_half_rejects_disagreeing_noise():
    """Pure noise carries no 00,00/FF,FF signal and each half solves to a different mask,
    which is why no key is accepted. This is the core safety property."""
    rng = random.Random(11)
    left, right = Stats(), Stats()
    for i in range(30):
        body = bytes(rng.randrange(256) for _ in range(BLOCK * 60))
        (left if i % 2 == 0 else right).add(bytes(CIPHER_START) + body)

    used = (left.blocks + right.blocks) * BLOCK
    mask, reason = evaluate(Sample(left, right, b"DKIF", used))

    assert mask is None
    assert "disagree" in reason


def test_is_masked_matches_decrypt_video(tmp_path):
    """A sampler that disagrees with decrypt_video about which payloads are masked poisons
    the statistics."""
    usm = USM(tmp_path / "Cs_Test.usm", bytes([1, 2, 3, 4]), bytes([5, 6, 7, 0]))
    threshold = MASK_START + MIN_MASKED

    for size in (threshold - 1, threshold, threshold + BLOCK):
        data = bytearray(size)
        usm.decrypt_video(data)
        changed = any(data)
        assert changed == is_masked(size), f"size {size:#x}"
