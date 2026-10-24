"""Live capture for Windows 10, whose PktMonApi.dll lacks the PacketMonitor* API. It is a port of
the Rust pktmon crate's legacy backend. It starts the driver through the undocumented Pktmon*
exports and reads packets from the driver's ETW provider. It has not been tested on Windows 10
itself."""

import contextlib
import ctypes
import functools
import queue
import threading
import uuid

from ctypes import (
    POINTER,
    c_int32,
    c_int64,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
    c_wchar,
    c_wchar_p,
)

from capture.net import ETHERNET, IP, UDP
from utils.errors import CharlotteError
from utils.strings import translate


# PktmonGetStatus and PktmonStop report into a buffer of this size in the crate.
STATUS_SIZE = 0x103C

SESSION_NAME = "charlotte-pktmon"

CONTROL_QUERY = 0
CONTROL_STOP = 1
WNODE_FLAG_TRACED_GUID = 0x00020000
REAL_TIME_MODE = 0x00000100
INDEPENDENT_SESSION_MODE = 0x08000000
PROCESS_TRACE_REAL_TIME = 0x00000100
PROCESS_TRACE_EVENT_RECORD = 0x10000000


class Guid(ctypes.Structure):
    _fields_ = (
        ("data1", c_uint32),
        ("data2", c_uint16),
        ("data3", c_uint16),
        ("data4", c_uint8 * 8),
    )


class Filter(ctypes.Structure):
    """The filter `pktmon filter add` gives the driver, laid out as the pktmon crate pins it."""

    _fields_ = (
        ("size", c_uint16),
        ("name", c_wchar * 64),
        ("mac_1", c_uint8 * 6),
        ("mac_2", c_uint8 * 6),
        ("vlan_id", c_uint16),
        ("ether_type", c_uint16),
        ("transport_protocol", c_uint8),
        ("ip_v6", c_uint8),
        ("reserved_1", c_uint8 * 4),
        ("ip_1", c_uint16 * 8),
        ("ip_2", c_uint16 * 8),
        ("ip_1_prefix", c_uint8),
        ("ip_2_prefix", c_uint8),
        ("port_1", c_uint16),
        ("port_2", c_uint16),
        ("tcp_flags", c_uint8),
        ("encapsulation", c_uint8),
        ("vxlan_port", c_uint16),
        ("heartbeat", c_uint8),
        ("reserved_2", c_uint8),
        ("dscp", c_uint16),
        ("reserved_3", c_uint8 * 2),
    )


class StartOptions(ctypes.Structure):
    """The crate leaves `packet_size` unnamed. Zero gives whole packets, as `--pkt-size 0` does."""

    _fields_ = (
        ("capture_type", c_uint32),
        ("packet_size", c_uint32),
        ("components", c_uint32),
        ("component_count", c_uint16),
        ("reserved_1", c_uint16),
        ("component_list", c_void_p),
        ("reserved_2", c_uint16),
    )


class WnodeHeader(ctypes.Structure):
    _fields_ = (
        ("buffer_size", c_uint32),
        ("provider_id", c_uint32),
        ("historical_context", c_uint64),
        ("timestamp", c_int64),
        ("guid", Guid),
        ("client_context", c_uint32),
        ("flags", c_uint32),
    )


class TraceProperties(ctypes.Structure):
    """EVENT_TRACE_PROPERTIES followed by room for the session name that ETW writes back."""

    _fields_ = (
        ("wnode", WnodeHeader),
        ("buffer_size", c_uint32),
        ("minimum_buffers", c_uint32),
        ("maximum_buffers", c_uint32),
        ("maximum_file_size", c_uint32),
        ("log_file_mode", c_uint32),
        ("flush_timer", c_uint32),
        ("enable_flags", c_uint32),
        ("age_limit", c_int32),
        ("number_of_buffers", c_uint32),
        ("free_buffers", c_uint32),
        ("events_lost", c_uint32),
        ("buffers_written", c_uint32),
        ("log_buffers_lost", c_uint32),
        ("real_time_buffers_lost", c_uint32),
        ("logger_thread_id", c_void_p),
        ("log_file_name_offset", c_uint32),
        ("logger_name_offset", c_uint32),
        ("logger_name", c_wchar * 64),
    )


class TraceLogfile(ctypes.Structure):
    """EVENT_TRACE_LOGFILEW, with byte blobs for the EVENT_TRACE and TRACE_LOGFILE_HEADER that a
    real-time consumer never reads. A blob adds no padding, and the fields after them stay aligned
    only because both sizes are multiples of 8."""

    _fields_ = (
        ("log_file_name", c_wchar_p),
        ("logger_name", c_wchar_p),
        ("current_time", c_int64),
        ("buffers_read", c_uint32),
        ("process_trace_mode", c_uint32),
        ("current_event", c_uint8 * 88),
        ("logfile_header", c_uint8 * 280),
        ("buffer_callback", c_void_p),
        ("buffer_size", c_uint32),
        ("filled", c_uint32),
        ("events_lost", c_uint32),
        ("event_record_callback", c_void_p),
        ("is_kernel_trace", c_uint32),
        ("context", c_void_p),
    )


class EventDescriptor(ctypes.Structure):
    _fields_ = (
        ("id", c_uint16),
        ("version", c_uint8),
        ("channel", c_uint8),
        ("level", c_uint8),
        ("opcode", c_uint8),
        ("task", c_uint16),
        ("keyword", c_uint64),
    )


class EventHeader(ctypes.Structure):
    _fields_ = (
        ("size", c_uint16),
        ("header_type", c_uint16),
        ("flags", c_uint16),
        ("event_property", c_uint16),
        ("thread_id", c_uint32),
        ("process_id", c_uint32),
        ("timestamp", c_int64),
        ("provider_id", Guid),
        ("descriptor", EventDescriptor),
        ("processor_time", c_uint64),
        ("activity_id", Guid),
    )


class EventRecord(ctypes.Structure):
    _fields_ = (
        ("header", EventHeader),
        ("buffer_context", c_uint32),
        ("extended_data_count", c_uint16),
        ("user_data_length", c_uint16),
        ("extended_data", c_void_p),
        ("user_data", c_void_p),
        ("user_context", c_void_p),
    )


class PropertyDescriptor(ctypes.Structure):
    _fields_ = (("name", c_void_p), ("array_index", c_uint32), ("reserved", c_uint32))


RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, POINTER(EventRecord))
BUFFER_CALLBACK = ctypes.WINFUNCTYPE(c_uint32, c_void_p)


@functools.cache
def etw():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.StartTraceW.argtypes = (POINTER(c_uint64), c_wchar_p, c_void_p)
    advapi.ControlTraceW.argtypes = (c_uint64, c_wchar_p, c_void_p, c_uint32)
    advapi.EnableTraceEx2.argtypes = (
        c_uint64,
        c_void_p,
        c_uint32,
        c_uint8,
        c_uint64,
        c_uint64,
        c_uint32,
        c_void_p,
    )
    advapi.OpenTraceW.argtypes = (c_void_p,)
    advapi.OpenTraceW.restype = c_uint64
    advapi.ProcessTrace.argtypes = (POINTER(c_uint64), c_uint32, c_void_p, c_void_p)
    advapi.CloseTrace.argtypes = (c_uint64,)
    for function in (
        "StartTraceW",
        "ControlTraceW",
        "EnableTraceEx2",
        "ProcessTrace",
        "CloseTrace",
    ):
        getattr(advapi, function).restype = c_uint32
    return advapi


@functools.cache
def tdh():
    library = ctypes.WinDLL("tdh")
    library.TdhGetProperty.argtypes = (
        c_void_p,
        c_uint32,
        c_void_p,
        c_uint32,
        POINTER(PropertyDescriptor),
        c_uint32,
        c_void_p,
    )
    library.TdhGetProperty.restype = c_uint32
    return library


@functools.cache
def property_descriptor(name: str) -> tuple[PropertyDescriptor, ctypes.Array]:
    """The descriptor points at the name's buffer and is returned with it to keep it alive."""
    buffer = ctypes.create_unicode_buffer(name)
    return PropertyDescriptor(ctypes.addressof(buffer), 0, 0), buffer


def read_property(record, name: str, size: int) -> bytes | None:
    """Fields are looked up by name rather than offset, because Windows 10 may lay the event out
    differently."""
    descriptor, _ = property_descriptor(name)
    buffer = ctypes.create_string_buffer(size)
    if tdh().TdhGetProperty(record, 0, None, 1, ctypes.byref(descriptor), size, buffer):
        return None
    return buffer.raw


def read_u32(record, name: str) -> int | None:
    data = read_property(record, name, 4)
    return None if data is None else int.from_bytes(data, "little")


def check(error: int):
    if error:
        raise ctypes.WinError(error)


def trace_properties() -> TraceProperties:
    properties = TraceProperties()
    properties.wnode.buffer_size = ctypes.sizeof(TraceProperties)
    properties.wnode.flags = WNODE_FLAG_TRACED_GUID
    # Asks for QPC timestamps because they are the cheapest to take.
    properties.wnode.client_context = 1
    properties.log_file_mode = REAL_TIME_MODE | INDEPENDENT_SESSION_MODE
    # Each buffer is 1 MB and not the crate's 16 MB, because ETW takes them from non-paged memory.
    # Up to 64 of them hold events while the consumer catches up.
    properties.buffer_size = 1024
    properties.maximum_buffers = 64
    properties.flush_timer = 1
    properties.logger_name_offset = TraceProperties.logger_name.offset
    return properties


def stop_session():
    """Stops the session by name, including one a crashed run left behind. ETW would otherwise
    keep that one running until the next reboot."""
    error = etw().ControlTraceW(0, SESSION_NAME, ctypes.byref(trace_properties()), CONTROL_STOP)
    # Error 4201 means no session by that name is running.
    if error != 4201:
        check(error)


class LegacyCapture:
    """The same interface as `capture.pktmon.Capture`. Everything `record` and `buffer` touch is
    a queue or a counter, because ETW calls them on the thread that runs ProcessTrace."""

    def __init__(self, ports: tuple[int, ...]):
        self.packets = queue.SimpleQueue()
        self.missed = 0
        self.oversized = 0
        self.failed = False
        self.closing = False
        self.capturing = False
        self.session = c_uint64()
        self.logfile = None
        self.trace = None
        self.thread = None
        self.on_record = RECORD_CALLBACK(self.record)
        self.on_buffer = BUFFER_CALLBACK(self.buffer)
        # The GUID, level, keyword and event ID are as `wevtutil gp Microsoft-Windows-PktMon`
        # lists them.
        provider_id = uuid.UUID("4d4f80d9-c8bd-4d73-bb5b-19c90402c5ac")
        self.provider = Guid.from_buffer_copy(provider_id.bytes_le)
        try:
            self.api = api = ctypes.WinDLL("PktMonApi.dll")
            for name in (
                "PktmonGetStatus",
                "PktmonStop",
                "PktmonRemoveAllFilters",
                "PktmonAddFilter",
                "PktmonStart",
            ):
                getattr(api, name).restype = c_uint32
        except (OSError, AttributeError) as e:
            raise CharlotteError(translate("CAPTURE_PKTMON_MISSING")) from e

        try:
            self.reset()
            self.start_trace()
            self.start_driver(ports)
        except OSError as e:
            self.close()
            raise CharlotteError(translate("CAPTURE_START_FAILED", error=e)) from e

    def reset(self):
        """Stops whatever an earlier run or another tool left in the driver, because the driver
        runs one capture at a time."""
        status = ctypes.create_string_buffer(STATUS_SIZE)
        check(self.api.PktmonGetStatus(status))
        if status.raw[0]:
            check(self.api.PktmonStop(status))
        check(self.api.PktmonRemoveAllFilters())
        stop_session()

    def start_trace(self):
        properties = trace_properties()
        check(etw().StartTraceW(ctypes.byref(self.session), SESSION_NAME, ctypes.byref(properties)))
        check(self.set_provider(True))
        self.logfile = TraceLogfile(
            logger_name=SESSION_NAME,
            process_trace_mode=PROCESS_TRACE_REAL_TIME | PROCESS_TRACE_EVENT_RECORD,
            buffer_callback=ctypes.cast(self.on_buffer, c_void_p),
            event_record_callback=ctypes.cast(self.on_record, c_void_p),
        )
        trace = etw().OpenTraceW(ctypes.byref(self.logfile))
        if trace == 0xFFFFFFFFFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        self.trace = trace
        self.thread = threading.Thread(target=self.consume, daemon=True)
        self.thread.start()

    def start_driver(self, ports: tuple[int, ...]):
        for port in ports:
            port_filter = Filter(
                size=ctypes.sizeof(Filter), name=f"UDP {port}", transport_protocol=UDP, port_1=port
            )
            check(self.api.PktmonAddFilter(ctypes.byref(port_filter)))
        options = StartOptions(capture_type=1, components=1)
        check(self.api.PktmonStart(ctypes.byref(options), None))
        self.capturing = True

    def set_provider(self, enabled: bool) -> int:
        """Level 4 is Informational and keyword 0x10 is Payload."""
        return etw().EnableTraceEx2(
            self.session,
            ctypes.byref(self.provider),
            enabled,
            4,
            0x10,
            0,
            0,
            None,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def next_frame(self, timeout: float) -> tuple[int, bytes] | None:
        try:
            return self.packets.get(timeout=timeout)
        except queue.Empty:
            return None

    def consume(self):
        handle = c_uint64(self.trace)
        etw().ProcessTrace(ctypes.byref(handle), 1, None, None)
        if not self.closing:
            self.failed = True

    def record(self, pointer):
        # Event 160 is the one that carries a captured packet.
        if pointer.contents.header.descriptor.id != 160:
            return
        packet_type = read_u32(pointer, "PacketType")
        if packet_type not in (ETHERNET, IP):
            return
        logged = read_u32(pointer, "LoggedPayloadSize")
        original = read_u32(pointer, "OriginalPayloadSize")
        if not logged or original is None:
            return
        # A cut packet would only fail to reassemble later, and the counter says why.
        if logged < original:
            self.oversized += 1
            return
        frame = read_property(pointer, "Payload", logged)
        if frame is not None:
            self.packets.put((packet_type, frame))

    def buffer(self, _) -> int:
        properties = trace_properties()
        if not etw().ControlTraceW(self.session, None, ctypes.byref(properties), CONTROL_QUERY):
            self.missed = properties.events_lost + properties.real_time_buffers_lost
        return 0 if self.closing else 1

    def close(self):
        """Releases only the handles that exist, because a failed start calls it too."""
        self.closing = True
        if self.capturing:
            with contextlib.suppress(OSError):
                check(self.api.PktmonStop(ctypes.create_string_buffer(STATUS_SIZE)))
            self.capturing = False
        self.api.PktmonRemoveAllFilters()
        if self.session.value:
            self.set_provider(False)
        # ProcessTrace only returns once the trace is closed, even when no event ever arrived.
        if self.trace is not None:
            etw().CloseTrace(self.trace)
            self.trace = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        if self.session.value:
            with contextlib.suppress(OSError):
                stop_session()
            self.session = c_uint64()
