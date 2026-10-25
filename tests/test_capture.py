import base64
import ctypes
import struct

from types import SimpleNamespace

import numpy as np
import pytest

from Crypto.Cipher import PKCS1_v1_5

import capture.run
import capture.sniffer

from capture.crypto import dotnet_random, load_rsa_keys, session_key, xor
from capture.match import Candidate, match_keys, unlisted_stream_files
from capture.net import ETHERNET, IP, udp
from capture.pktmon_legacy import Filter
from capture.proto import FoundKey, find_keys
from capture.run import KeyCapture
from capture.sniffer import GAP_SECONDS, RECOVERY_TRIES, Kcp, Sniffer
from conftest import chunk, forbid_call, video_header
from stages.usm import decrypt_stream
from utils.errors import Cancelled
from utils.strings import translate


AES = b"f9e9e1c5cf3a68deffa0a4d3df665836"
AUDIO = b"5281416956962385"
VIDEO_KEY = b"9602627639787478"
CLIENT_PORT, SERVER_PORT = 50000, 22101


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def protobuf(*fields: tuple[int, int | bytes]) -> bytes:
    """A protobuf message from (field number, value) pairs, where an int becomes a varint and
    bytes become a length-delimited field."""
    out = b""
    for number, value in fields:
        if isinstance(value, int):
            out += varint(number << 3) + varint(value)
        else:
            out += varint(number << 3 | 2) + varint(len(value)) + value
    return out


def game_message(command_id: int, header: bytes, body: bytes) -> bytes:
    return (
        b"\x45\x67"
        + struct.pack(">HHI", command_id, len(header), len(body))
        + header
        + body
        + b"\x89\xab"
    )


def kcp_segment(conv: int, sn: int, frg: int, data: bytes) -> bytes:
    return struct.pack("<IIBBHIIII", conv, 0, 81, frg, 256, 0, sn, 0, len(data)) + bytes(4) + data


# Values of (ulong)(new System.Random(seed).NextDouble() * ulong.MaxValue) taken from .NET 10.
# Seeds past 161803398 make C# overflow its seed table, and most real clock values are past it.
SYSTEM_RANDOM = {
    0: [
        13396823736352909312,
        15076991733327230976,
        14167517994065479680,
        10296256650306605056,
        3800640862925172224,
    ],
    161803399: [
        5270011658755223552,
        8730442312260356096,
        8992329532393805824,
        4047350441721604096,
        9225443873244755968,
    ],
    2147483647: [
        13396823736352909312,
        15076991733327230976,
        14167518045605087232,
        10296256650306605056,
        3800640862925172224,
    ],
    -2147483648: [
        13396823736352909312,
        15076991733327230976,
        14167518045605087232,
        10296256650306605056,
        3800640862925172224,
    ],
    1790000000: [
        2301991103225962752,
        5599184987630377984,
        13335063644205961216,
        5646702332561996800,
        7123900345515400192,
    ],
    -877905643: [
        1824955304549750272,
        17725438211892367360,
        18401043688923547648,
        14343836735339698176,
        16235912792390178816,
    ],
}


@pytest.mark.parametrize("seed, expected", SYSTEM_RANDOM.items())
def test_dotnet_random_matches_system_random(seed, expected):
    assert dotnet_random(np.array([seed]), 5)[:, 0].tolist() == expected


def test_session_key_matches_auto_artifactarium():
    """Reference bytes from auto-artifactarium's `new_key_from_seed`. The middle slice crosses the
    point where the generator refills its state."""
    key = session_key(0x0123456789ABCDEF ^ 13396823736352909312)
    assert key[:16].hex() == "9f26a37bf7b4cbcb47c0cbcdd5b14e14"
    assert key[2480:2500].hex() == "40b1d1cc3b758a12e5b3bf3fb45e2b66e2922673"
    assert key[4080:].hex() == "c80ce29ee63db129cd71c90b8d4aa428"


@pytest.mark.parametrize(
    "body",
    [
        protobuf((1, protobuf((22, AES), (2, 7017)))),
        protobuf((1, protobuf((22, AES), (23, AES[::-1]), (645, AUDIO)))),
        protobuf((1, protobuf((22, AES))), (645, AUDIO)),
    ],
    ids=["no decimal sibling", "two hex strings", "decimal in another message"],
)
def test_only_one_hex_and_one_decimal_side_by_side_count(body):
    assert find_keys(body) == []


@pytest.mark.parametrize(
    "children, parent",
    [([701701, 701702], 7017), ([], None), ([701701, 100], None)],
    ids=["child quests name it", "no child quests", "two candidates"],
)
def test_parent_quest_is_the_number_its_child_quests_derive_from(children, parent):
    record = protobuf(
        *[(13, protobuf((3, 3), (5, child))) for child in children],
        (4, 1),
        (14, 7017),
        (15, 1790145541),
        (22, AES),
        (652, AUDIO),
    )
    assert find_keys(protobuf((4, record))) == [FoundKey(int(AUDIO), AES.decode(), parent)]


@pytest.mark.parametrize(
    "children, found",
    [
        ([60201, 60219], [FoundKey(int(VIDEO_KEY), None, 602)]),
        ([], []),
    ],
    ids=["parent quest record", "a lone number"],
)
def test_an_old_key_counts_only_inside_a_parent_quest_record(children, found):
    """Finished quests from before 7.1 send their videoKey alone, and many other messages also
    carry numbers as strings."""
    record = protobuf(
        *[(13, protobuf((3, 3), (5, child))) for child in children],
        (4, 1),
        (14, 602),
        (652, VIDEO_KEY),
    )
    assert find_keys(protobuf((4, record))) == found


def test_a_number_shorter_than_eight_digits_is_not_a_key():
    """The name gets the message past the search for 8 digits."""
    record = protobuf(
        (13, protobuf((3, 3), (5, 60201))),
        (4, 1),
        (14, 602),
        (16, b"Cs_20261023"),
        (652, b"5"),
    )
    assert find_keys(protobuf((4, record))) == []


def test_ethernet_and_bare_ip_give_back_the_udp_payload_and_wifi_does_not():
    payload = b"\x00\x00\x00\xff" + bytes(16)
    datagram = struct.pack(">HHHH", SERVER_PORT, CLIENT_PORT, 8 + len(payload), 0) + payload
    packet = bytes([0x45, 0, 0, 20 + len(datagram), 0, 0, 0, 0, 64, 17]) + bytes(10) + datagram
    frame = bytes(12) + b"\x08\x00" + packet

    ipv6 = bytes([0x60, 0, 0, 0]) + struct.pack(">HBB", len(datagram), 17, 64) + bytes(32)

    assert udp(ETHERNET, frame) == (payload, SERVER_PORT, CLIENT_PORT)
    assert udp(IP, packet) == (payload, SERVER_PORT, CLIENT_PORT)
    assert udp(IP, ipv6 + datagram) == (payload, SERVER_PORT, CLIENT_PORT)
    assert udp(2, frame) is None


def test_the_windows_10_filter_matches_the_crates_pinned_layout():
    """The driver reads the filter by offset. These come from the pktmon crate's own tests, the
    only record of the undocumented layout."""
    assert ctypes.sizeof(Filter) == 0xC8
    offsets = {name: getattr(Filter, name).offset for name, _ in Filter._fields_}
    assert offsets == {
        "size": 0x00,
        "name": 0x02,
        "mac_1": 0x82,
        "mac_2": 0x88,
        "vlan_id": 0x8E,
        "ether_type": 0x90,
        "transport_protocol": 0x92,
        "ip_v6": 0x93,
        "reserved_1": 0x94,
        "ip_1": 0x98,
        "ip_2": 0xA8,
        "ip_1_prefix": 0xB8,
        "ip_2_prefix": 0xB9,
        "port_1": 0xBA,
        "port_2": 0xBC,
        "tcp_flags": 0xBE,
        "encapsulation": 0xBF,
        "vxlan_port": 0xC0,
        "heartbeat": 0xC2,
        "reserved_2": 0xC3,
        "dscp": 0xC4,
        "reserved_3": 0xC6,
    }


def login(reporter, clock_offset_ms: int = 42, session_messages: int = 1):
    """A sniffer that has seen the handshake, the session key, and the packets of a login in the
    order the game sends them. These are the client's hello, the server's reply with its half of
    the seed, and then the client's first messages under the session key, numbered from 300. The
    client seeded its half `clock_offset_ms` after the server's timestamp."""
    dispatch = bytes((i * 37 + 11) % 256 for i in range(4096))
    rsa_keys = load_rsa_keys()
    server = 0x0123456789ABCDEF
    sent_ms = 1790123456789
    client = int(dotnet_random(np.array([sent_ms + clock_offset_ms]), 5)[3, 0])
    session = session_key(client ^ server)
    sealed = PKCS1_v1_5.new(rsa_keys[1].public_key()).encrypt(server.to_bytes(8))
    token_reply = game_message(100, protobuf((6, sent_ms)), protobuf((2, base64.b64encode(sealed))))
    hello = game_message(99, b"", protobuf((1, 5)))

    sniffer = Sniffer({int.from_bytes(dispatch[:2]): dispatch}, rsa_keys, reporter)
    sniffer.feed(b"\x00\x00\x00\xff" + bytes(16), CLIENT_PORT, SERVER_PORT, now=0.0)
    packets = [
        (kcp_segment(7, 0, 0, xor(hello, dispatch)), CLIENT_PORT, SERVER_PORT),
        (kcp_segment(7, 0, 0, xor(token_reply, dispatch)), SERVER_PORT, CLIENT_PORT),
    ]
    for sn in range(1, session_messages + 1):
        message = game_message(299 + sn, b"", protobuf((1, sn)))
        packets.append((kcp_segment(7, sn, 0, xor(message, session)), CLIENT_PORT, SERVER_PORT))
    return sniffer, session, packets


def replay(sniffer, packets) -> list[int]:
    return [command.id for packet in packets for command in sniffer.feed(*packet, now=0.0)]


def logged_in(reporter):
    sniffer, session, packets = login(reporter)
    commands = [command for packet in packets for command in sniffer.feed(*packet, now=0.0)]
    return sniffer, session, commands


def test_login_yields_the_video_key(reporter):
    """The key record is nested like the server's, and its message arrives split, reordered and
    repeated."""
    sniffer, session, commands = logged_in(reporter)
    record = protobuf(
        (13, protobuf((3, 3), (5, 701701))),
        (13, protobuf((3, 4), (5, 701702))),
        (14, 7017),
        (15, 1790145541),
        (22, AES.upper()),
        (645, AUDIO),
    )
    notify = xor(game_message(200, b"", protobuf((3, protobuf((5, record))))), session)
    half = len(notify) // 2
    for segment in (
        kcp_segment(7, 2, 0, notify[half:]),
        kcp_segment(7, 1, 1, notify[:half]),
        kcp_segment(7, 1, 1, notify[:half]),
    ):
        commands += sniffer.feed(segment, SERVER_PORT, CLIENT_PORT, now=0.0)

    assert [command.id for command in commands] == [99, 100, 300, 200]
    assert find_keys(commands[-1].body) == [FoundKey(int(AUDIO), AES.decode(), 7017)]
    assert reporter.logs == [
        ("info", translate("CAPTURE_LOGIN_DETECTED")),
        ("info", translate("CAPTURE_SESSION_KEY_FOUND")),
    ]


def test_traffic_before_the_login_warns_once(reporter):
    sniffer = Sniffer({}, [], reporter)
    for _ in range(2):
        sniffer.feed(kcp_segment(7, 0, 0, bytes(40)), SERVER_PORT, CLIENT_PORT, now=0.0)

    assert reporter.logs == [("warning", translate("CAPTURE_LOGIN_MISSED"))]


def test_a_message_that_fails_to_decrypt_leaves_the_session_running(reporter):
    sniffer, session, _ = logged_in(reporter)
    garbage = kcp_segment(7, 1, 0, bytes(range(40)))
    good = kcp_segment(7, 2, 0, xor(game_message(201, b"", b""), session))

    assert sniffer.feed(garbage, SERVER_PORT, CLIENT_PORT, now=0.0) == []
    commands = sniffer.feed(good, SERVER_PORT, CLIENT_PORT, now=0.0)
    assert [command.id for command in commands] == [201]
    assert sniffer.broken == 1


def test_messages_that_overtake_the_login_reply_wait_for_it(reporter):
    """Each direction is reassembled on its own. The client's first message under the session key
    can then arrive before the server's reply that holds the seed."""
    sniffer, _, packets = login(reporter)
    hello, reply, first = packets

    assert replay(sniffer, [hello, first, reply]) == [99, 100, 300]
    assert reporter.logs[-1] == ("info", translate("CAPTURE_SESSION_KEY_FOUND"))


def test_a_message_too_short_to_be_one_does_not_reach_the_key_search(reporter):
    sniffer, _, packets = login(reporter)
    hello, reply, first = packets
    empty = (kcp_segment(7, 1, 0, b""), SERVER_PORT, CLIENT_PORT)

    assert replay(sniffer, [hello, reply, empty, first]) == [99, 100, 300]
    assert reporter.logs[-1] == ("info", translate("CAPTURE_SESSION_KEY_FOUND"))


@pytest.mark.parametrize(
    "drop, decrypted, error",
    [
        (1, [99], translate("CAPTURE_LOGIN_REPLY_MISSED")),
        (None, [99, 100], translate("CAPTURE_SESSION_KEY_OUT_OF_WINDOW", seconds=3)),
    ],
    ids=["reply never captured", "clock too far off"],
)
def test_a_lost_login_names_its_cause_and_only_then_says_to_log_in_again(
    reporter, monkeypatch, drop, decrypted, error
):
    monkeypatch.setattr(capture.sniffer, "SESSION_WINDOW_MS", 3000)
    sniffer, _, packets = login(reporter, clock_offset_ms=5000, session_messages=RECOVERY_TRIES)
    if drop is not None:
        del packets[drop]

    assert replay(sniffer, packets) == decrypted
    sniffer.finish()
    assert reporter.logs[1:] == [
        ("error", error),
        ("warning", translate("CAPTURE_NOTHING_DECRYPTED")),
    ]


def test_a_segment_the_capture_missed_costs_only_its_own_message():
    """Message B spans segments 1 and 2, and segment 1 never arrives. Nothing moves before
    GAP_SECONDS, because a retransmission would have filled the gap by then."""
    kcp = Kcp(7)
    assert kcp.feed(kcp_segment(7, 0, 0, b"A"), now=0.0) == [b"A"]
    assert kcp.feed(kcp_segment(7, 2, 0, b"B2"), now=0.0) == []
    assert kcp.feed(kcp_segment(7, 3, 0, b"C"), now=GAP_SECONDS - 1) == []
    assert kcp.feed(kcp_segment(7, 4, 0, b"D"), now=GAP_SECONDS) == [b"C", b"D"]
    assert kcp.skipped == 1


def test_a_late_retransmission_fills_the_gap():
    kcp = Kcp(7)
    assert kcp.feed(kcp_segment(7, 1, 0, b"B"), now=0.0) == []
    assert kcp.feed(kcp_segment(7, 0, 0, b"A"), now=1.0) == [b"A", b"B"]
    assert kcp.skipped == 0


def test_a_segment_the_capture_cut_short_waits_for_its_retransmission():
    kcp = Kcp(7)
    segment = kcp_segment(7, 0, 0, b"ABCD")
    assert kcp.feed(segment[:-1], now=0.0) == []
    assert kcp.feed(segment, now=1.0) == [b"ABCD"]


def test_gaps_skipped_before_a_relog_still_count(reporter):
    """The summary reads the count after the capture, when only the last connection is left."""
    sniffer, _, _ = logged_in(reporter)
    sniffer.feed(kcp_segment(7, 2, 0, bytes(40)), SERVER_PORT, CLIENT_PORT, now=0.0)
    sniffer.feed(kcp_segment(7, 3, 0, bytes(40)), SERVER_PORT, CLIENT_PORT, now=GAP_SECONDS)
    sniffer.feed(b"\x00\x00\x00\xff" + bytes(16), CLIENT_PORT, SERVER_PORT, now=GAP_SECONDS)

    assert sniffer.skipped == 1


def stream_file(path, aes_key: str, half: int = 256, nonce: int = 0x1122334455667788):
    """A 7.1 file whose frames each hold two frames of `half` bytes behind a superframe index,
    encrypted the way the game encrypts them. The first payload starts with the IVF file header."""
    marker = 0xC0 | 1 << 3 | 1  # two frames, with two bytes for each size
    frame = bytes(2 * half) + bytes([marker]) + struct.pack("<HH", half, half) + bytes([marker])

    chunks = [video_header(nonce)]
    for frame_time in range(4):
        file_header = bytes(32) if frame_time == 0 else b""
        payload = bytearray(file_header + struct.pack("<IQ", len(frame), frame_time) + frame)
        decrypt_stream(payload, bytes.fromhex(aes_key), nonce, frame_time)
        chunks.append(chunk(b"@SFV", bytes(payload), frame_time=frame_time))
    path.write_bytes(b"".join(chunks))
    return Candidate(path, nonce)


def test_a_key_matches_the_file_it_decrypts_and_no_other(tmp_path):
    other_key = AES[::-1].decode()
    ours = stream_file(tmp_path / "Cs_Ours.usm", AES.decode())
    theirs = stream_file(tmp_path / "Cs_Theirs.usm", other_key)

    matched = match_keys([ours, theirs], {1: AES.decode(), 2: "00" * 16}, lambda: None)
    assert list(matched) == [(1, "Cs_Ours")]


def test_frames_inside_the_clear_part_match_no_key(tmp_path):
    """Their superframe index is never encrypted, and any key would pass it."""
    tiny = stream_file(tmp_path / "Cs_Tiny.usm", AES.decode(), half=20)
    assert list(match_keys([tiny], {2: "00" * 16}, lambda: None)) == []


def test_only_unlisted_files_with_the_71_scheme_are_candidates(tmp_path):
    stream_file(tmp_path / "Cs_New.usm", AES.decode())
    stream_file(tmp_path / "Cs_Listed.usm", AES.decode())
    (tmp_path / "Cs_Old.usm").write_bytes(video_header())

    stems = [c.path.stem for c in unlisted_stream_files(tmp_path, {"Cs_Listed"})]
    assert stems == ["Cs_New"]


@pytest.fixture
def key_capture(reporter, tmp_path):
    capture_run = KeyCapture({"list": []}, reporter)
    capture_run.candidates = [Candidate(tmp_path / "Cs_Ours.usm", 0)]
    return capture_run


def reported_videos(reporter):
    return [data["videos"] for kind, data in reporter.events]


def test_a_new_key_waits_for_its_files_and_goes_out_once(key_capture, reporter, monkeypatch):
    def found(candidates, aes_keys, checkpoint):
        yield int(AUDIO), "Cs_Ours"

    monkeypatch.setattr(capture.run, "match_keys", found)
    key_capture.add_key(FoundKey(int(AUDIO), AES.decode(), 7099))
    assert reported_videos(reporter) == []

    key_capture.match_pending()
    assert reported_videos(reporter) == [["Cs_Ours"]]
    assert key_capture.candidates == []


def test_a_stop_during_matching_still_reports_the_key(key_capture, reporter, monkeypatch):
    def stopped(candidates, aes_keys, checkpoint):
        raise Cancelled

    monkeypatch.setattr(capture.run, "match_keys", stopped)
    key_capture.add_key(FoundKey(int(AUDIO), AES.decode(), 7099))
    with pytest.raises(Cancelled):
        key_capture.match_pending()
    assert reported_videos(reporter) == [[]]


def test_the_count_comes_before_what_went_wrong(key_capture, reporter):
    key_capture.packets = 1
    key_capture.summarize(SimpleNamespace(missed=3, oversized=0, failed=True))
    assert reporter.logs == [
        ("info", translate("CAPTURE_SUMMARY", keys=0)),
        ("warning", translate("CAPTURE_LOSSY_HINT")),
        ("error", translate("CAPTURE_STOPPED_BY_PKTMON")),
    ]


def test_a_new_old_scheme_key_is_not_matched(key_capture, reporter, monkeypatch):
    monkeypatch.setattr(capture.run, "match_keys", forbid_call)
    key_capture.add_key(FoundKey(int(VIDEO_KEY), None, 602))
    key_capture.match_pending()
    assert reported_videos(reporter) == [[]]


def test_keys_json_groups_are_known_and_each_key_is_reported_once(reporter):
    """The GUI's "only new keys" filter hides what this marks known, and the server sends the
    whole list again at every login."""
    groups = [
        {"audioKey": int(AUDIO), "aesKey": AES.decode().upper(), "videos": ["Cs_A"]},
        {"videoKey": int(VIDEO_KEY), "parentQuestId": "602", "videos": ["Cs_B"]},
    ]
    key_capture = KeyCapture({"list": [{"version": "7.1", "videoGroups": groups}]}, reporter)

    for _ in range(2):
        key_capture.add_key(FoundKey(int(AUDIO), AES.decode(), 7017))
    key_capture.add_key(FoundKey(int(VIDEO_KEY), None, None))

    assert [data for kind, data in reporter.events] == [
        {
            "video_key": None,
            "audio_key": int(AUDIO),
            "aes_key": AES.decode(),
            "parent_quest_id": "7017",
            "known": True,
            "videos": ["Cs_A"],
        },
        {
            "video_key": int(VIDEO_KEY),
            "audio_key": None,
            "aes_key": None,
            "parent_quest_id": "602",
            "known": True,
            "videos": ["Cs_B"],
        },
    ]
