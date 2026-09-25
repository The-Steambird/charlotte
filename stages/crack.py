"""Recover a USM decryption key from the file's own video stream.

Against the running XOR of the ciphertext, every even block is its plaintext XOR video_mask2
(see USM.decrypt_video), which makes the even blocks a repeating-key XOR. Compressed VP9 has
enough 00 00 / FF FF byte pairs to rank candidates for it. Only 7 key bytes are free
(USM.build_mask), and a beam search fixes one of them per stage.

A key is accepted only when two halves of the video, each built from its own distinct
payloads, solve to the same 56 bits. Parsing the decrypted output proves nothing
because IVF frame lengths are never masked, and the stream parses under any key.
"""

from contextlib import closing
from typing import TYPE_CHECKING, NamedTuple

import numpy as np

from resources.keys import DecryptionKey
from stages.usm import BLOCK, CIPHER_START, is_masked, read_chunks, video_nonce
from utils.logger import log


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from utils.reporter import Reporter


SAMPLE_STEPS = (10_000_000, 30_000_000)  # combined budget per step, each pool sees half of it
MIN_SAMPLE_BYTES = 100_000
BEAM = 50
WIDE_BEAM = 300  # the 16-bit stage scores too few entries to trust a narrow beam
BIGRAM_WEIGHT = 25  # total weight shared between the 00,00 and FF,FF terms
BIGRAM_MIN_HITS = 100  # plaintext pairs needed before measuring how to share it
BIGRAM_RATIO = (1.0, 5.0)  # clamped so that one lopsided file cannot zero out either term
BIGRAM_FALLBACK = (10, 4)  # zero/ff weights when there are too few hits to measure


# These mirror `USM.build_mask` line for line, vectorized over a (32, n) array of
# candidates `m` with `value` holding the key byte tried for each. Any change to
# `build_mask` lands here too.
def expand_0(m: np.ndarray, value: np.ndarray) -> None:
    m[0x00] = value
    m[0x07] = m[0x00] ^ 0xFF
    m[0x09] = (m[0x01] - m[0x07]) & 0xFF
    m[0x0C] = (m[0x0B] + m[0x09]) & 0xFF
    m[0x11] = m[0x10] ^ m[0x07]


def expand_1_2(m: np.ndarray, value: np.ndarray) -> None:
    m[0x01] = value >> 8
    m[0x02] = value & 0xFF
    m[0x08] = (m[0x02] + m[0x01]) & 0xFF
    m[0x0A] = m[0x02] ^ 0xFF
    m[0x0B] = m[0x01] ^ 0xFF
    m[0x0F] = (m[0x0A] - m[0x0B]) & 0xFF
    m[0x10] = (m[0x08] - m[0x0F]) & 0xFF
    m[0x12] = m[0x0F] ^ 0xFF


def expand_3(m: np.ndarray, value: np.ndarray) -> None:
    m[0x03] = value
    m[0x0D] = (m[0x08] - m[0x03]) & 0xFF
    m[0x0E] = m[0x0D] ^ 0xFF
    m[0x13] = m[0x03] ^ 0x10
    m[0x17] = (m[0x13] - m[0x0F]) & 0xFF
    m[0x19] = (0x21 - m[0x13]) & 0xFF
    m[0x1C] = (m[0x17] + 0x44) & 0xFF


def expand_4(m: np.ndarray, value: np.ndarray) -> None:
    m[0x04] = value
    m[0x14] = (m[0x04] - 0x32) & 0xFF
    m[0x1A] = m[0x14] ^ m[0x17]
    m[0x1D] = (m[0x03] + m[0x04]) & 0xFF
    m[0x1F] = m[0x1D] ^ m[0x13]


def expand_5(m: np.ndarray, value: np.ndarray) -> None:
    m[0x05] = value
    m[0x15] = (m[0x05] + 0xED) & 0xFF
    m[0x18] = (m[0x15] + m[0x07]) & 0xFF
    m[0x1E] = (m[0x05] - m[0x16]) & 0xFF


def expand_6(m: np.ndarray, value: np.ndarray) -> None:
    m[0x06] = value
    m[0x16] = m[0x06] ^ 0xF3
    m[0x1B] = (m[0x16] + m[0x16]) & 0xFF


class Stage(NamedTuple):
    expand: Callable[[np.ndarray, np.ndarray], None]
    span: int
    beam: int
    entries: tuple[int, ...]  # mask entries this step determines
    pairs: tuple[int, ...]  # adjacent entry pairs that first become scorable here


def plan_stages() -> list[Stage]:
    """Order the expansions by dependency (6 before 5, since `expand_5` reads m[0x16])
    and note where each adjacent pair first becomes scorable."""
    order = [
        (expand_1_2, 0x10000, WIDE_BEAM, (0x01, 0x02, 0x08, 0x0A, 0x0B, 0x0F, 0x10, 0x12)),
        (expand_0, 0x100, BEAM, (0x00, 0x07, 0x09, 0x0C, 0x11)),
        (expand_3, 0x100, BEAM, (0x03, 0x0D, 0x0E, 0x13, 0x17, 0x19, 0x1C)),
        (expand_4, 0x100, BEAM, (0x04, 0x14, 0x1A, 0x1D, 0x1F)),
        (expand_6, 0x100, BEAM, (0x06, 0x16, 0x1B)),
        (expand_5, 0x100, BEAM, (0x05, 0x15, 0x18, 0x1E)),
    ]

    known: set[int] = set()
    scored: set[int] = set()
    stages = []
    for expand, span, beam, entries in order:
        known.update(entries)
        pairs = tuple(j for j in range(BLOCK - 1) if j not in scored and {j, j + 1} <= known)
        scored.update(pairs)
        stages.append(Stage(expand, span, beam, entries, pairs))
    return stages


STAGES = plan_stages()


class Stats:
    def __init__(self):
        self.evens: list[np.ndarray] = []  # plaintext ^ video_mask2 rows, counted in tables()
        self.zero_pairs = 0
        self.ff_pairs = 0
        self.blocks = 0

    def add(self, payload: bytes) -> int:
        rows = (len(payload) - CIPHER_START) // BLOCK
        body = np.frombuffer(payload, dtype=np.uint8, count=rows * BLOCK, offset=CIPHER_START)
        running = np.bitwise_xor.accumulate(body.reshape(rows, BLOCK), axis=0)
        odd = running[1::2]  # plaintext with no key involved
        # Counted once in tables(), because a bincount per payload would mostly be zeroing
        # a 65536-wide table.
        self.evens.append(running[0::2])

        # Odd blocks measure this file's 00,00 vs FF,FF split.
        left, right = odd[:, :-1], odd[:, 1:]
        self.zero_pairs += int(np.count_nonzero((left == 0) & (right == 0)))
        self.ff_pairs += int(np.count_nonzero((left == 0xFF) & (right == 0xFF)))

        self.blocks += rows
        return rows * BLOCK

    def tables(self) -> tuple[np.ndarray, np.ndarray]:
        """Score tables where each candidate is summed with its complement, since plaintext
        FF lands the running XOR on v and plaintext 00 lands it on v ^ FF."""
        even = np.concatenate(self.evens)
        pairs = (even[:, :-1].astype(np.int32) << 8) | even[:, 1:]
        unigram = np.stack([np.bincount(even[:, j], minlength=256) for j in range(BLOCK)])
        bigram = np.stack([np.bincount(pairs[:, j], minlength=65536) for j in range(BLOCK - 1)])

        byte_flip = np.arange(256) ^ 0xFF
        pair_flip = np.arange(65536) ^ 0xFFFF
        unigram = unigram + unigram[:, byte_flip]

        if self.zero_pairs + self.ff_pairs < BIGRAM_MIN_HITS:
            zero_weight, ff_weight = BIGRAM_FALLBACK
        else:
            low, high = BIGRAM_RATIO
            ratio = min(max(self.zero_pairs / max(self.ff_pairs, 1), low), high)
            zero_weight = round(BIGRAM_WEIGHT * ratio / (1.0 + ratio))
            ff_weight = BIGRAM_WEIGHT - zero_weight

        bigram = ff_weight * bigram + zero_weight * bigram[:, pair_flip]
        return unigram, bigram


def solve(unigram: np.ndarray, bigram: np.ndarray) -> list[int]:
    masks = np.zeros((BLOCK, 1), dtype=np.int32)
    scores = np.zeros(1, dtype=np.int64)

    for stage in STAGES:
        candidates = np.repeat(masks, stage.span, axis=1)
        values = np.tile(np.arange(stage.span, dtype=np.int32), masks.shape[1])
        stage.expand(candidates, values)

        total = np.repeat(scores, stage.span)
        for j in stage.entries:
            total += unigram[j][candidates[j]]
        for j in stage.pairs:
            total += bigram[j][(candidates[j] << 8) | candidates[j + 1]]

        width = min(stage.beam, total.size)
        keep = np.argpartition(-total, width - 1)[:width]
        masks, scores = candidates[:, keep], total[keep]

    return masks[:, int(np.argmax(scores))].tolist()


def key_from_mask(mask: list[int]) -> DecryptionKey:
    """Invert `USM.build_mask`. Byte 7 is never read by the mask and is always zero
    anyway, because a real key is only 56 bits (see `Keys.decryption_key`)."""
    key1 = bytes([mask[0x00], mask[0x01], mask[0x02], (mask[0x03] + 0x34) & 0xFF])
    key2 = bytes([(mask[0x04] - 0xF9) & 0xFF, mask[0x05] ^ 0x13, (mask[0x06] - 0x61) & 0xFF, 0])
    return DecryptionKey(key1, key2)


class Sample(NamedTuple):
    left: Stats
    right: Stats
    first_payload: bytes | None  # None when the file carries no video
    used: int


class Recovery(NamedTuple):
    key: DecryptionKey | None
    reason: str


def decline(usm_file: Path, reason: str) -> Recovery:
    log.warning(f"Could not recover a key for {usm_file.name}: {reason}.")
    return Recovery(None, reason)


def collect(usm_file: Path, reporter: Reporter, budget: int) -> Sample:
    pools = (Stats(), Stats())
    seen: set[int] = set()  # payload digests, used to deal a repeated frame only once
    first: bytes | None = None
    pool = 0
    used = 0
    file_size = usm_file.stat().st_size

    # closing() drops the file handle if the budget ends the walk early.
    with (
        reporter.task("crack", total=file_size, unit="B") as task,
        closing(read_chunks(usm_file)) as chunks,
    ):
        for count, (header, payload) in enumerate(chunks, start=1):
            task.advance(header.data_size + 8)
            if count % 100 == 0:
                reporter.checkpoint()

            if header.signature != b"@SFV" or not header.is_data:
                continue
            if first is None:
                first = payload
            if not is_masked(len(payload)):
                continue
            digest = hash(payload)
            if digest not in seen:
                seen.add(digest)
                used += pools[pool].add(payload)
                pool ^= 1
                if used >= budget:
                    break

        task.set_completed(file_size)

    return Sample(pools[0], pools[1], first, used)


def evaluate(sample: Sample) -> tuple[list[int] | None, str]:
    if not sample.right.blocks:
        # A placeholder asset loops one frame, which is dealt once and leaves nothing to
        # confirm it against.
        return None, "only one distinct video payload, so there is no second half to confirm it"

    left = solve(*sample.left.tables())
    right = solve(*sample.right.tables())
    if left != right:
        return None, f"independent halves of the video disagree ({sample.used} bytes sampled)"
    return left, ""


def crack_key(usm_file: Path, reporter: Reporter) -> Recovery:
    if video_nonce(usm_file) is not None:
        return decline(usm_file, "the video uses the 7.1 encryption that cannot be cracked")

    log.info(f"Recovering decryption key from {usm_file.name}...")
    reason = ""

    for budget in SAMPLE_STEPS:
        sample = collect(usm_file, reporter, budget)
        first = sample.first_payload
        if first is None or first[:4] != b"DKIF":
            return decline(usm_file, "no IVF video stream in this file")
        if sample.used < MIN_SAMPLE_BYTES:
            return decline(usm_file, f"only {sample.used} bytes of encrypted video")

        mask, reason = evaluate(sample)
        if mask is not None:
            blocks = sample.left.blocks + sample.right.blocks
            log.info(f"Recovered decryption key from {blocks} blocks.")
            return Recovery(key_from_mask(mask), "")

        if sample.used < budget:
            break  # the whole file was already sampled, and more budget adds nothing

        log.info(f"{sample.used} bytes were inconclusive, retrying with more video...")

    return decline(usm_file, f"inconclusive - {reason}")
