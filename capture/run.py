import contextlib
import time

from capture.crypto import load_dispatch_keys, load_rsa_keys
from capture.match import match_keys, unlisted_stream_files
from capture.net import udp
from capture.pktmon import Capture, realtime_available
from capture.pktmon_legacy import LegacyCapture
from capture.proto import FoundKey, find_keys
from capture.sniffer import PORTS, Sniffer
from resources.game import game_video_dir
from resources.keys import groups
from utils.errors import Cancelled
from utils.strings import translate
lazy from utils.reporter import Reporter


def known_groups(data: dict) -> dict[str | int, dict]:
    known = {}
    for _, group in groups(data):
        aes_key = group.get("aesKey")
        video_key = group.get("videoKey")
        if isinstance(aes_key, str):
            known[aes_key.strip().lower()] = group
        elif isinstance(video_key, int) and video_key:
            known[video_key] = group
    return known


def listed_stems(data: dict) -> set[str]:
    return {stem for _, group in groups(data) for stem in group.get("videos", [])}


class KeyCapture:
    def __init__(self, data: dict, reporter: Reporter, legacy: bool = False):
        self.reporter = reporter
        self.legacy = legacy
        self.known = known_groups(data)
        self.listed = listed_stems(data)
        self.sniffer = Sniffer(load_dispatch_keys(), load_rsa_keys(), reporter)
        self.found = set()
        self.candidates = []
        self.pending = {}
        self.last_key = 0.0
        self.packets = 0

    def run(self):
        self.scan_game_folder()
        with self.open_capture() as capture:
            self.reporter.log("info", translate("CAPTURE_STARTED"))
            with contextlib.suppress(Cancelled, KeyboardInterrupt):
                self.loop(capture)
            # Waiting keys are still matched after Ctrl+C or a PktMon failure. A stop from the
            # GUI stays set, and the keys are reported without their files.
            with contextlib.suppress(Cancelled, KeyboardInterrupt):
                self.match_pending()
            self.summarize(capture)

    def open_capture(self) -> Capture | LegacyCapture:
        if not self.legacy and realtime_available():
            return Capture(PORTS)
        capture = LegacyCapture(PORTS)
        self.reporter.log("info", translate("CAPTURE_LEGACY_BACKEND"))
        return capture

    def scan_game_folder(self):
        """Lists the files new keys are matched against."""
        video_dir = game_video_dir()
        if video_dir is None:
            self.reporter.log("info", translate("CAPTURE_NO_GAME_FOLDER"))
            return
        self.candidates = unlisted_stream_files(video_dir, self.listed)
        self.reporter.log("info", translate("CAPTURE_UNLISTED_FILES", count=len(self.candidates)))

    def loop(self, capture: Capture | LegacyCapture):
        warned_missed = warned_oversized = False
        while not capture.failed:
            self.reporter.checkpoint()
            entry = capture.next_frame(timeout=0.5)
            now = time.monotonic()
            if entry is not None:
                self.packets += 1
                packet = udp(*entry)
                if packet is not None:
                    self.feed(*packet, now)

            if capture.missed and not warned_missed:
                self.reporter.log("warning", translate("CAPTURE_PACKETS_MISSED"))
                warned_missed = True
            if capture.oversized and not warned_oversized:
                self.reporter.log("warning", translate("CAPTURE_PACKETS_OVERSIZED"))
                warned_oversized = True
            # Files are read two seconds after the last key. All of a login's keys arrive in one
            # message, and the login burst is mostly over by then.
            if self.pending and now - self.last_key >= 2:
                self.match_pending()

    def feed(self, payload: bytes, src: int, dst: int, now: float):
        for command in self.sniffer.feed(payload, src, dst, now):
            if command.from_server:
                for key in find_keys(command.body):
                    self.add_key(key)

    def add_key(self, key: FoundKey):
        """Reports a key the first time it arrives, because the server sends the whole list at
        every login."""
        if (key.number, key.aes_key) in self.found:
            return
        self.found.add((key.number, key.aes_key))

        group = self.known.get(key.aes_key or key.number)
        quest_id = group.get("parentQuestId") if group else None
        if key.parent_quest is not None:
            quest_id = str(key.parent_quest)
        quest_name = quest_id or translate("CAPTURE_QUEST_UNKNOWN")

        if group:
            self.reporter.log("info", translate("CAPTURE_KEY_KNOWN", quest=quest_name))
            self.report_key(key, quest_id, known=True, videos=list(group["videos"]))
            return

        self.reporter.log("info", translate("CAPTURE_KEY_NEW", quest=quest_name))
        if key.aes_key is None or not self.candidates:
            self.report_key(key, quest_id, known=False, videos=[])
            return
        # The key goes out with its files once the burst is over.
        self.pending[key.number] = (key, quest_id)
        self.last_key = time.monotonic()

    def report_key(self, key: FoundKey, quest_id: str | None, known: bool, videos: list):
        old_scheme = key.aes_key is None
        self.reporter.event(
            "capture_key",
            video_key=key.number if old_scheme else None,
            audio_key=None if old_scheme else key.number,
            aes_key=key.aes_key,
            parent_quest_id=quest_id,
            known=known,
            videos=videos,
        )

    def match_pending(self):
        """A stop partway through still reports every key, with the files found up to then."""
        if not self.pending:
            return
        pending, self.pending = self.pending, {}
        aes_keys = {number: key.aes_key for number, (key, _) in pending.items()}
        videos = {number: [] for number in pending}
        self.reporter.log(
            "info", translate("CAPTURE_MATCHING", keys=len(pending), files=len(self.candidates))
        )
        try:
            for number, stem in match_keys(self.candidates, aes_keys, self.reporter.checkpoint):
                videos[number].append(stem)
        finally:
            matched = {stem for stems in videos.values() for stem in stems}
            self.candidates = [
                candidate for candidate in self.candidates if candidate.path.stem not in matched
            ]
            for number, (key, quest_id) in pending.items():
                self.report_key(key, quest_id, known=False, videos=videos[number])

    def summarize(self, capture: Capture | LegacyCapture):
        """The count goes first, because the GUI's status bar shows the last line and that should
        be what went wrong."""
        self.reporter.log("info", translate("CAPTURE_SUMMARY", keys=len(self.found)))
        self.sniffer.finish()
        if not self.packets:
            self.reporter.log("warning", translate("CAPTURE_NO_TRAFFIC"))
        elif capture.missed or capture.oversized or self.sniffer.skipped or self.sniffer.broken:
            self.reporter.log("warning", translate("CAPTURE_LOSSY_HINT"))
        if capture.failed:
            self.reporter.log("error", translate("CAPTURE_STOPPED_BY_PKTMON"))
