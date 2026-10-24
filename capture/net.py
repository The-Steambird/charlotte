import struct


# PktMon's payload types.
ETHERNET = 1
IP = 3

UDP = 17


def udp(packet_type: int, frame: bytes) -> tuple[bytes, int, int] | None:
    """A frame's UDP (payload, source port, destination port), or None if it has no UDP."""
    if packet_type == ETHERNET:
        pos = 18 if frame[12:14] == b"\x81\x00" else 14
    elif packet_type == IP:
        pos = 0
    else:
        return None
    if len(frame) < pos + 20:
        return None
    match frame[pos] >> 4:
        case 4:
            protocol = frame[pos + 9]
            # An IPv4 header states its own length in 4-byte words.
            start = pos + (frame[pos] & 0x0F) * 4
        case 6:
            protocol = frame[pos + 6]
            start = pos + 40
        case _:
            return None
    if protocol != UDP or len(frame) < start + 8:
        return None
    src, dst, length = struct.unpack_from(">HHH", frame, start)
    return frame[start + 8 : start + length], src, dst
