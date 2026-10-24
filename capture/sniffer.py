import struct

from dataclasses import dataclass

from capture.crypto import FRAMING_SIZE, MAGIC_START, find_session_key, parse, server_seeds, xor
from capture.proto import fields
from utils.strings import translate
lazy from utils.reporter import Reporter


PORTS = (22101, 22102)

# The game never resends a segment it received, even when the capture missed it. A gap still
# open after this long is skipped.
GAP_SECONDS = 3.0
SESSION_WINDOW_MS = 10_000  # +/- 10s search
RECOVERY_TRIES = 3


@dataclass
class Command:
    id: int
    body: bytes
    from_server: bool


class Kcp:
    """Segment header is 24 bytes + token after `conv` + 4 unknown bytes before the data.
    Only segments with `cmd` 81 carry data, others are acknowledgements and window probes.
    Repeated segments are dropped by sequence number."""

    def __init__(self, conv: int):
        self.conv = conv
        self.next = 0
        self.pending = {}
        self.parts = []
        self.gap_since = None
        self.skipped = 0

    def feed(self, data: bytes, now: float) -> list[bytes]:
        pos = 0
        while pos + 32 <= len(data):
            conv, _, cmd, frg, _, _, sn, _, size = struct.unpack_from("<IIBBHIIII", data, pos)
            end = pos + 32 + size
            # A segment the capture cut short is treated as missed.
            if end > len(data):
                break
            if conv == self.conv and cmd == 81 and sn >= self.next:
                self.pending[sn] = (frg, data[pos + 32 : end])
            pos = end

        messages = self.drain()
        if not self.pending:
            self.gap_since = None
        elif self.gap_since is None or messages:
            self.gap_since = now
        elif now - self.gap_since >= GAP_SECONDS:
            self.skip_gap()
            messages += self.drain()
        return messages

    def drain(self) -> list[bytes]:
        messages = []
        while self.next in self.pending:
            frg, part = self.pending.pop(self.next)
            self.next += 1
            self.parts.append(part)
            if frg == 0:
                messages.append(b"".join(self.parts))
                self.parts = []
        return messages

    def skip_gap(self):
        """Drops the message the gap fell in, up to the first pending segment with `frg` 0."""
        ends = [sn for sn, (frg, _) in self.pending.items() if frg == 0]
        if not ends:
            return
        end = min(ends)
        self.pending = {sn: part for sn, part in self.pending.items() if sn > end}
        self.next = end + 1
        self.parts = []
        self.gap_since = None
        self.skipped += 1


class Sniffer:
    def __init__(self, dispatch_keys: dict[int, bytes], rsa_keys: list, reporter: Reporter):
        self.dispatch_keys = dispatch_keys
        self.rsa_keys = rsa_keys
        self.reporter = reporter
        self.logged_in = False
        self.warned_login_missed = False
        self.decrypted = False
        self.broken = 0
        self.kcp = {}
        self.skipped_earlier = 0
        self.reset()

    def reset(self):
        self.skipped_earlier = self.skipped
        self.kcp = {}
        self.key = None
        self.session = False
        self.dead = False
        self.seeds = []
        self.sent_ms = 0
        self.waiting = []
        self.waiting_bytes = 0
        self.tried = 0

    def report_unrecovered(self):
        """Reports a connection that ended, or a capture that stopped, still waiting for its
        session key."""
        if not self.waiting or self.session or self.dead:
            return
        if self.seeds:
            self.report_out_of_window()
        else:
            self.reporter.log("error", translate("CAPTURE_LOGIN_REPLY_MISSED"))
        self.waiting = []

    def finish(self):
        self.report_unrecovered()
        if self.logged_in and not self.decrypted:
            self.reporter.log("warning", translate("CAPTURE_NOTHING_DECRYPTED"))

    @property
    def skipped(self) -> int:
        return self.skipped_earlier + sum(kcp.skipped for kcp in self.kcp.values())

    def feed(self, payload: bytes, src: int, dst: int, now: float) -> list[Command]:
        if src not in PORTS and dst not in PORTS:
            return []
        from_server = src in PORTS
        # Packets of 20 bytes or less are handshakes. The client's opening one holds 255 in its
        # first 4 bytes.
        if len(payload) <= 20:
            if int.from_bytes(payload[:4]) == 0xFF:
                self.report_unrecovered()
                self.reset()
                if not self.logged_in:
                    self.reporter.log("info", translate("CAPTURE_LOGIN_SEEN"))
                self.logged_in = True
            return []
        if not self.logged_in:
            if not self.warned_login_missed:
                self.reporter.log("warning", translate("CAPTURE_LOGIN_MISSED"))
                self.warned_login_missed = True
            return []
        if self.dead:
            return []

        kcp = self.kcp.get(from_server)
        if kcp is None:
            kcp = self.kcp[from_server] = Kcp(int.from_bytes(payload[:4], "little"))
        commands = []
        for message in kcp.feed(payload, now):
            commands += self.decrypt(message, from_server)
        return commands

    def decrypt(self, message: bytes, from_server: bool) -> list[Command]:
        # A message < 12 parses under no key, which will only pick a wrong dispatch key or use
        # up a key search try.
        if self.dead or len(message) < FRAMING_SIZE:
            return []
        if self.key is None:
            # gi.json lists each dispatch key under its first two bytes. The first message gives
            # them away, because every message starts with the same magic.
            key_id = int.from_bytes(xor(message[:2], MAGIC_START))
            self.key = self.dispatch_keys.get(key_id)
            if self.key is None:
                self.reporter.log("error", translate("CAPTURE_NO_DISPATCH_KEY", key_id=key_id))
                self.dead = True
                return []

        parsed = parse(xor(message, self.key))
        if parsed is not None:
            commands = [self.command(parsed, from_server)]
            if self.waiting and self.seeds:
                commands += self.recover()
            return commands
        # Under the session key, a bad message lost a part, and the ones after it still decrypt.
        if self.session:
            self.broken += 1
            return []

        # No key decrypts this message yet and waits until the session key is found.
        # A whole login should be about 2 MB.
        if self.waiting_bytes + len(message) <= 32 * 2**20:
            self.waiting.append((message, from_server))
            self.waiting_bytes += len(message)
        return self.recover() if self.seeds else []

    def recover(self) -> list[Command]:
        """Tries more than one waiting message (missing part finds no key)."""
        while self.tried < min(len(self.waiting), RECOVERY_TRIES):
            message, _ = self.waiting[self.tried]
            self.tried += 1
            key = find_session_key(message, self.seeds, self.sent_ms, SESSION_WINDOW_MS)
            if key is not None:
                return self.start_session(key)
        if self.tried == RECOVERY_TRIES:
            self.report_out_of_window()
            self.dead = True
            self.waiting = []
        return []

    def report_out_of_window(self):
        seconds = SESSION_WINDOW_MS // 1000
        self.reporter.log("error", translate("CAPTURE_SESSION_KEY_OUT_OF_WINDOW", seconds=seconds))

    def start_session(self, key: bytes) -> list[Command]:
        self.key = key
        self.session = True
        self.decrypted = True
        self.reporter.log("info", translate("CAPTURE_SESSION_KEY_FOUND"))
        commands = []
        for message, from_server in self.waiting:
            parsed = parse(xor(message, key))
            if parsed is None:
                self.broken += 1
            else:
                commands.append(self.command(parsed, from_server))
        self.waiting, self.waiting_bytes = [], 0
        return commands

    def command(self, parsed: tuple[int, bytes, bytes], from_server: bool) -> Command:
        command_id, header, body = parsed
        if from_server and not self.session:
            seeds = server_seeds(body, self.rsa_keys)
            if seeds:
                self.seeds = seeds
                self.sent_ms = dict(fields(header).varints).get(6, 0)
        return Command(command_id, body, from_server)
