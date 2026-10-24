"""Decrypts the game's network traffic. Each message is XORed with a 4096-byte key.

A login starts under a fixed dispatch key in gi.json. After that, both sides use a session key built
from a seed in two halves. The server sends its half in the login reply, locked with RSA, where
private keys are in secrets/. Client's half cannot be read from traffic and is determined with
C#'s random number generator seeded with its clock in ms, so it can be found by trying every ms
near the server's reply timestamp.
"""

import base64
import struct

import orjson
lazy import numpy as np

lazy from Crypto.Cipher import PKCS1_v1_5
lazy from Crypto.PublicKey import RSA

from capture.proto import fields
from utils.paths import bundle_root


SECRETS = bundle_root() / "capture" / "secrets"
# A message longer than the key reuses it from the start.
KEY_SIZE = 4096
# Every game message starts and ends with these bytes.
MAGIC_START = b"\x45\x67"
MAGIC_END = b"\x89\xab"
# The bytes of a message besides its header and body. No message is shorter.
FRAMING_SIZE = 12
# 3000ms clock search at a time.
CHUNK_MS = 3000
# The client seeds C#'s Random with its clock and takes one of the numbers it draws as its half,
# which may not be the first one. The search then tries the first 5 draws for every clock value.
CLIENT_DRAWS = 5
# The standard sizes of MT19937-64, the generator that turns a session seed into its key. Each
# word it makes comes from the words 312, 311 and 156 places before it, as mersenne lays out.
MT_N = 312
MT_M = 156


def load_dispatch_keys() -> dict[int, bytes]:
    data = orjson.loads((SECRETS / "gi.json").read_bytes())
    return {int(version): base64.b64decode(key) for version, key in data.items()}


def load_rsa_keys() -> list:
    paths = sorted(SECRETS.glob("private_key_*.pem"))
    return [RSA.import_key(path.read_bytes()) for path in paths]


def xor(data: bytes, key: bytes) -> bytes:
    """XORs `data` with `key` repeated to its length. The same call encrypts and decrypts."""
    stream = np.resize(np.frombuffer(key, np.uint8), len(data))
    return (np.frombuffer(data, np.uint8) ^ stream).tobytes()


def parse(plain: bytes) -> tuple[int, bytes, bytes] | None:
    """The command id, header and body of a decrypted message, or None on wrong key.
    Message byte format:

        magic | command id | header size | body size | header | body | magic
          2   |     2      |      2      |     4     |  ...   | ...  |   2
    """
    if len(plain) < FRAMING_SIZE or plain[:2] != MAGIC_START or plain[-2:] != MAGIC_END:
        return None
    command, header_size, body_size = struct.unpack_from(">HHI", plain, 2)
    if FRAMING_SIZE + header_size + body_size != len(plain):
        return None
    header_end = 10 + header_size
    return command, plain[10:header_end], plain[header_end:-2]


def server_seeds(body: bytes, rsa_keys: list) -> list[int]:
    """The server's half of the session seed from the body of the login reply. It is an 8-byte
    number, locked with RSA and then base64 encoded."""
    seeds = []
    for value in fields(body).strings:
        try:
            blob = base64.b64decode(value, validate=True)
        except ValueError:
            continue
        for key in rsa_keys:
            try:
                seed = PKCS1_v1_5.new(key).decrypt(blob, None)
            except ValueError:
                continue
            if seed is not None and len(seed) == 8:
                seeds.append(int.from_bytes(seed))
    return seeds


def dotnet_random(seeds: np.ndarray, count: int) -> np.ndarray:
    """A port of .NET's `new Random(seed)` with a column for each seed, giving the first `count`
    draws as the client's `(ulong)(NextDouble() * ulong.MaxValue)`. The table is int32 because
    C# lets it overflow and the numbers depend on that."""
    big = 2**31 - 1

    def positive(values: np.ndarray) -> np.ndarray:
        return np.where(values < 0, values + big, values)

    seeds = seeds.astype(np.int32)
    subtraction = np.where(seeds == -(2**31), big, np.abs(seeds))
    table = np.zeros((56, len(seeds)), np.int32)
    mj = 161803398 - subtraction
    table[55] = mj
    mk = np.ones(len(seeds), np.int32)
    for i in range(1, 55):
        ii = 21 * i % 55
        table[ii] = mk
        mk = positive(mj - mk)
        mj = table[ii]
    for _ in range(4):
        for i in range(1, 56):
            table[i] = positive(table[i] - table[1 + (i + 30) % 55])

    samples = np.empty((count, len(seeds)), np.int32)
    for n in range(count):
        inext = n % 55 + 1
        inextp = (n + 21) % 55 + 1
        sample = positive(table[inext] - table[inextp])
        sample[sample == big] -= 1
        table[inext] = sample
        samples[n] = sample

    doubles = samples * (1.0 / big)
    return (doubles * float(2**64 - 1)).astype(np.uint64)


def temper(word: np.ndarray) -> np.ndarray:
    """Scrambles one number of the state into the number the generator hands out."""
    x = word ^ ((word >> 29) & 0x5555555555555555)
    x ^= (x << 17) & 0x71D67FFFEDA60000
    x ^= (x << 37) & 0xFFF7EEE000000000
    return x ^ (x >> 43)


def mersenne(seeds: np.ndarray, outputs: list[int]) -> np.ndarray:
    """The numbers at the given positions of MT19937-64, with a row for each position and a
    column for each seed.

    The reference code keeps 312 words and refills them all at once. This treats the words as one
    growing list instead. The seed makes the first 312, and each word after that is made from
    three of the words before it. Number i is word 312 + i after temper. The search needs only
    a few numbers near the start. Only the words those numbers depend on are made."""
    needed = set()
    pending = [MT_N + position for position in outputs]
    while pending:
        j = pending.pop()
        if j not in needed:
            needed.add(j)
            if j >= MT_N:
                pending += [j - MT_N, j - MT_N + 1, j - MT_M]

    words = {}
    word = seeds
    for i in range(max(j for j in needed if j < MT_N) + 1):
        if i:
            word = 6364136223846793005 * (word ^ (word >> 62)) + i
        if i in needed:
            words[i] = word
    for j in sorted(j for j in needed if j >= MT_N):
        y = (words[j - MT_N] & 0xFFFFFFFF80000000) | (words[j - MT_N + 1] & 0x7FFFFFFF)
        words[j] = words[j - MT_M] ^ (y >> 1) ^ ((y & 1) * 0xB5026F5AA96619E9)
    return np.stack([temper(words[MT_N + position]) for position in outputs])


def session_words(seeds: np.ndarray, words: list[int]) -> np.ndarray:
    """The given 8-byte words of each seed's session key. The game seeds one generator with the
    session seed and uses its first number to seed a second one. The key is that second
    generator's numbers, starting from its second."""
    return mersenne(mersenne(seeds, [0])[0], [word + 1 for word in words])


def session_key(seed: int) -> bytes:
    words = session_words(np.array([seed], np.uint64), list(range(KEY_SIZE // 8)))
    return words[:, 0].astype(">u8").tobytes()


def matching_seeds(message: bytes, candidates: np.ndarray) -> np.ndarray:
    """The candidate seeds whose key turns the first two and last two bytes of `message` into the
    magic bytes. Only the key words under those four bytes are made as it is cheaper than
    whole keys. find_session_key checks each in full, because a wrong seed can still pass."""
    ends = [0, 1, len(message) - 2, len(message) - 1]
    words = sorted({end % KEY_SIZE // 8 for end in ends})
    key_words = dict(zip(words, session_words(candidates, words), strict=True))

    match = np.ones(len(candidates), bool)
    for end, magic in zip(ends, MAGIC_START + MAGIC_END, strict=True):
        position = end % KEY_SIZE
        # A word's first byte sits in its top 8 bits, because the words are big-endian.
        shift = 8 * (7 - position % 8)
        key_byte = (key_words[position // 8] >> shift) & 0xFF
        match &= key_byte == (message[end] ^ magic)
    return candidates[match]


def find_session_key(
    message: bytes, seeds: list[int], sent_ms: int, window_ms: int
) -> bytes | None:
    """The session key that decrypts `message`, or None. `seeds` are the server's halves and
    `sent_ms` is the timestamp, both from the login reply. The search tries every clock value
    within `window_ms` of `sent_ms`, nearest first."""
    offsets = np.arange(1 - window_ms, window_ms)
    offsets = offsets[np.argsort(np.abs(offsets), stable=True)]
    for start in range(0, len(offsets), CHUNK_MS):
        times = sent_ms + offsets[start : start + CHUNK_MS]
        # C# turns the clock into a 32-bit int that keeps only its low bits.
        clients = dotnet_random(times.astype(np.int32), CLIENT_DRAWS).ravel()
        # The session seed is the client's half XORed with the server's half.
        candidates = np.concatenate([clients ^ server for server in seeds])
        for seed in matching_seeds(message, candidates):
            key = session_key(int(seed))
            if parse(xor(message, key)):
                return key
    return None
