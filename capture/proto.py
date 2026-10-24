import re

from typing import NamedTuple


def varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise IndexError


class Fields(NamedTuple):
    varints: list[tuple[int, int]]
    strings: list[bytes]


def fields(data: bytes) -> Fields:
    """Fixed-width fields are skipped, because keys and seeds never sit in them. Bytes that are
    not a message give no fields."""
    varints = []
    strings = []
    pos = 0
    try:
        while pos < len(data):
            tag, pos = varint(data, pos)
            number, wire = tag >> 3, tag & 7
            if number == 0:
                raise ValueError
            match wire:
                case 0:
                    value, pos = varint(data, pos)
                    varints.append((number, value))
                case 1:
                    pos += 8
                case 2:
                    size, pos = varint(data, pos)
                    strings.append(data[pos : pos + size])
                    pos += size
                case 5:
                    pos += 4
                case _:
                    raise ValueError
        if pos != len(data):
            raise ValueError
    except IndexError, ValueError:
        return Fields([], [])
    return Fields(varints, strings)


def parent_quest(record: Fields, nested: list[bytes]) -> int | None:
    """A key record lists its child quests, and a child's id is its parent's id times 100 plus a
    counter."""
    numbers = {value for _, value in record.varints}
    children = []
    for data in nested:
        children += [value for _, value in fields(data).varints]
    parents = {child // 100 for child in children} & numbers
    return parents.pop() if len(parents) == 1 else None


class FoundKey(NamedTuple):
    """`number` is the audio key when `aes_key` is set, and the old videoKey when it is None."""

    number: int
    aes_key: str | None
    parent_quest: int | None


def find_keys(data: bytes) -> list[FoundKey]:
    """Keys are found by their shape, because the field numbers come from a single 7.1 login and
    nothing pins them. A 7.1+ key is a message with one 32-character hex string and one decimal
    string. An old key is a lone decimal string. It counts only inside a parent quest record,
    because other messages carry numbers as strings too."""
    found = []
    # Every key in keys.json has 16 digits. Anything shorter than 8 is not a key.
    if not re.search(rb"[0-9]{8}", data):
        return found
    parsed = fields(data)
    hexes = []
    decimals = []
    nested = []
    for value in parsed.strings:
        if re.fullmatch(rb"[0-9a-fA-F]{32}", value):
            hexes.append(value)
        elif re.fullmatch(rb"[0-9]{1,20}", value):
            decimals.append(value)
        else:
            nested.append(value)
    numbers = [number for number in map(int, decimals) if number < 2**64]
    if len(numbers) == 1 and numbers[0] >= 10**7 and len(hexes) <= 1:
        aes_key = hexes[0].decode().lower() if hexes else None
        quest = parent_quest(parsed, nested)
        if aes_key is not None or quest is not None:
            found.append(FoundKey(numbers[0], aes_key, quest))
    for value in nested:
        found += find_keys(value)
    return found
