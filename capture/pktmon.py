"""Live capture through the Windows 11 Packet Monitor API in PktMonApi.dll. The structures are
ported from pktmon.h in the Rust pktmon crate."""

import contextlib
import ctypes
import queue
import threading

from ctypes import c_int, c_int64, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p, c_wchar

from capture.net import ETHERNET, IP, UDP
from utils.errors import CharlotteError
from utils.strings import translate


# For any size above this one, the stream silently falls back to 128 bytes and cuts every packet
# short (measured 2026-10-20).
TRUNCATION = 9000

STREAM_STARTED = 0
STREAM_STOPPED = 1
FATAL_ERROR = 2
PROCESS_INFO = 3


class DataDescriptor(ctypes.Structure):
    _fields_ = (
        ("data", c_void_p),
        ("data_size", c_uint32),
        ("metadata_offset", c_uint32),
        ("packet_offset", c_uint32),
        ("packet_length", c_uint32),
        ("missed_write_count", c_uint32),
        ("missed_read_count", c_uint32),
    )


class Metadata(ctypes.Structure):
    _fields_ = (
        ("group_id", c_uint64),
        ("count", c_uint16),
        ("appearance_count", c_uint16),
        ("direction", c_uint16),
        ("packet_type", c_uint16),
        ("component_id", c_uint16),
        ("edge_id", c_uint16),
        ("reserved", c_uint16),
        ("drop_reason", c_uint32),
        ("drop_location", c_uint32),
        ("processor", c_uint16),
        ("timestamp", c_int64),
    )


class StartInfo(ctypes.Structure):
    _fields_ = (("buffer_size", c_uint32), ("truncation_size", c_uint16))


class StreamConfiguration(ctypes.Structure):
    _fields_ = (
        ("context", c_void_p),
        ("event_callback", c_void_p),
        ("data_callback", c_void_p),
        ("buffer_size_multiplier", c_uint16),
        ("truncation_size", c_uint16),
    )


class Constraint(ctypes.Structure):
    _fields_ = (
        ("name", c_wchar * 64),
        ("flags", c_uint32),
        ("mac_1", c_uint8 * 6),
        ("mac_2", c_uint8 * 6),
        ("vlan_id", c_uint16),
        ("ether_type", c_uint16),
        ("dscp", c_uint16),
        ("transport_protocol", c_uint8),
        ("ip_1", c_uint16 * 8),
        ("ip_2", c_uint16 * 8),
        ("ip_1_prefix", c_uint8),
        ("ip_2_prefix", c_uint8),
        ("port_1", c_uint16),
        ("port_2", c_uint16),
        ("tcp_flags", c_uint8),
        ("encap_type", c_uint32),
        ("vxlan_port", c_uint16),
        ("packets", c_uint64),
        ("bytes", c_uint64),
    )


def realtime_available() -> bool:
    """Windows 10 PktMonApi.dll does not have PacketMonitor* functions."""
    try:
        return hasattr(ctypes.WinDLL("PktMonApi.dll"), "PacketMonitorInitialize")
    except OSError:
        return False


DATA_CALLBACK = ctypes.WINFUNCTYPE(None, c_void_p, ctypes.POINTER(DataDescriptor))
EVENT_CALLBACK = ctypes.WINFUNCTYPE(None, c_void_p, c_void_p, c_int)


class Capture:
    """Everything `data` and `event` touch is a queue or a counter, because PktMonApi.dll calls
    them on its own thread."""

    def __init__(self, ports: tuple[int, ...]):
        self.packets = queue.SimpleQueue()
        self.missed = 0
        self.oversized = 0
        self.failed = False
        self.closing = False
        self.started = threading.Event()
        self.truncation = None
        self.handle, self.session, self.stream = c_void_p(), c_void_p(), c_void_p()
        self.on_data = DATA_CALLBACK(self.data)
        self.on_event = EVENT_CALLBACK(self.event)
        # The caller has checked realtime_available().
        self.api = api = ctypes.WinDLL("PktMonApi.dll")
        for name in (
            "PacketMonitorInitialize",
            "PacketMonitorCreateLiveSession",
            "PacketMonitorCreateRealtimeStream",
            "PacketMonitorAttachOutputToSession",
            "PacketMonitorAddCaptureConstraint",
            "PacketMonitorSetSessionActive",
        ):
            getattr(api, name).restype = ctypes.HRESULT

        try:
            api.PacketMonitorInitialize(0x00010000, None, ctypes.byref(self.handle))
            api.PacketMonitorCreateLiveSession(self.handle, "charlotte", ctypes.byref(self.session))
            config = StreamConfiguration(
                event_callback=ctypes.cast(self.on_event, c_void_p),
                data_callback=ctypes.cast(self.on_data, c_void_p),
                buffer_size_multiplier=10,
                truncation_size=TRUNCATION,
            )
            api.PacketMonitorCreateRealtimeStream(
                self.handle, ctypes.byref(config), ctypes.byref(self.stream)
            )
            api.PacketMonitorAttachOutputToSession(self.session, self.stream)
            for port in ports:
                # These flag bits tell PktMon that transport_protocol and port_1 are set.
                constraint = Constraint(
                    name=f"UDP {port}",
                    flags=1 << 5 | 1 << 11,
                    transport_protocol=UDP,
                    port_1=port,
                )
                api.PacketMonitorAddCaptureConstraint(self.session, ctypes.byref(constraint))
            api.PacketMonitorSetSessionActive(self.session, 1)
        except OSError as e:
            self.close()
            raise CharlotteError(translate("CAPTURE_START_FAILED", error=e)) from e

        self.started.wait(2)
        if self.truncation != TRUNCATION:
            self.close()
            raise CharlotteError(
                translate(
                    "CAPTURE_TRUNCATION_REFUSED", expected=TRUNCATION, reported=self.truncation
                )
            )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def next_frame(self, timeout: float) -> tuple[int, bytes] | None:
        """The next (payload type, frame), or None on timeout."""
        try:
            return self.packets.get(timeout=timeout)
        except queue.Empty:
            return None

    def data(self, _, descriptor):
        d = descriptor.contents
        self.missed += d.missed_write_count + d.missed_read_count
        packet_type = Metadata.from_address(d.data + d.metadata_offset).packet_type
        if packet_type in (ETHERNET, IP):
            frame = ctypes.string_at(d.data + d.packet_offset, d.packet_length)
            self.packets.put((packet_type, frame))

    def event(self, _, info, kind):
        if kind == STREAM_STARTED and info:
            self.truncation = StartInfo.from_address(info).truncation_size
            self.started.set()
        elif kind == PROCESS_INFO and info:
            # The only process info seen so far is a dropped packet over the truncation size.
            self.oversized += 1
        elif kind == FATAL_ERROR or (kind == STREAM_STOPPED and not self.closing):
            self.failed = True

    def close(self):
        """Releases only the handles that exist, because a failed start calls it too."""
        self.closing = True
        if self.session.value:
            with contextlib.suppress(OSError):
                self.api.PacketMonitorSetSessionActive(self.session, 0)
        if self.stream.value:
            self.api.PacketMonitorCloseRealtimeStream(self.stream)
        if self.session.value:
            self.api.PacketMonitorCloseSessionHandle(self.session)
        if self.handle.value:
            self.api.PacketMonitorUninitialize(self.handle)
        self.handle, self.session, self.stream = c_void_p(), c_void_p(), c_void_p()
