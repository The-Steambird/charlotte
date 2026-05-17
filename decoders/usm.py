import struct

from pathlib import Path
from typing import BinaryIO

from tqdm import tqdm

from utils.logger import log


# USM chunk signatures
SIG_CRID = 0x43524944  # CRID - Container ID
SIG_VIDEO = 0x40534656  # @SFV - Video chunk
SIG_AUDIO = 0x40534641  # @SFA - Audio chunk
SIG_CUE = 0x40435545  # @CUE - Cue point

HEADER_SIZE = 32
VIDEO_OFFSET = 0x40
MASK_SIZE = 0x20
MIN_VIDEO_SIZE = 0x200


class ChunkHeader:
    __slots__ = (
        "channel_no",
        "data_offset",
        "data_size",
        "data_type",
        "frame_rate",
        "frame_time",
        "padding_size",
        "signature",
    )

    def __init__(self):
        self.signature = 0
        self.data_size = 0
        self.data_offset = 0
        self.padding_size = 0
        self.channel_no = 0
        self.data_type = 0
        self.frame_time = 0
        self.frame_rate = 0

    @classmethod
    def from_bytes(cls, data: bytes) -> ChunkHeader:
        header = cls()
        (
            header.signature,
            header.data_size,
            header.data_offset,
            header.padding_size,
            header.channel_no,
            header.data_type,
            header.frame_time,
            header.frame_rate,
        ) = struct.unpack(">I I x B H B 2x B I I 8x", data)
        return header


class USM:
    def __init__(self, file_path: Path, key1: bytes, key2: bytes):
        self.file_path = Path(file_path)
        self.key1 = key1
        self.key2 = key2
        self.video_mask1 = bytearray(MASK_SIZE)
        self.video_mask2 = bytearray(MASK_SIZE)
        self._init_masks(key1, key2)

    def _init_masks(self, key1: bytes, key2: bytes) -> None:
        m = self.video_mask1

        m[0x00] = key1[0]
        m[0x01] = key1[1]
        m[0x02] = key1[2]
        m[0x03] = (key1[3] - 0x34) & 0xFF
        m[0x04] = (key2[0] + 0xF9) & 0xFF
        m[0x05] = (key2[1] ^ 0x13) & 0xFF
        m[0x06] = (key2[2] + 0x61) & 0xFF
        m[0x07] = (m[0x00] ^ 0xFF) & 0xFF
        m[0x08] = (m[0x02] + m[0x01]) & 0xFF
        m[0x09] = (m[0x01] - m[0x07]) & 0xFF
        m[0x0A] = (m[0x02] ^ 0xFF) & 0xFF
        m[0x0B] = (m[0x01] ^ 0xFF) & 0xFF
        m[0x0C] = (m[0x0B] + m[0x09]) & 0xFF
        m[0x0D] = (m[0x08] - m[0x03]) & 0xFF
        m[0x0E] = (m[0x0D] ^ 0xFF) & 0xFF
        m[0x0F] = (m[0x0A] - m[0x0B]) & 0xFF
        m[0x10] = (m[0x08] - m[0x0F]) & 0xFF
        m[0x11] = (m[0x10] ^ m[0x07]) & 0xFF
        m[0x12] = (m[0x0F] ^ 0xFF) & 0xFF
        m[0x13] = (m[0x03] ^ 0x10) & 0xFF
        m[0x14] = (m[0x04] - 0x32) & 0xFF
        m[0x15] = (m[0x05] + 0xED) & 0xFF
        m[0x16] = (m[0x06] ^ 0xF3) & 0xFF
        m[0x17] = (m[0x13] - m[0x0F]) & 0xFF
        m[0x18] = (m[0x15] + m[0x07]) & 0xFF
        m[0x19] = (0x21 - m[0x13]) & 0xFF
        m[0x1A] = (m[0x14] ^ m[0x17]) & 0xFF
        m[0x1B] = (m[0x16] + m[0x16]) & 0xFF
        m[0x1C] = (m[0x17] + 0x44) & 0xFF
        m[0x1D] = (m[0x03] + m[0x04]) & 0xFF
        m[0x1E] = (m[0x05] - m[0x16]) & 0xFF
        m[0x1F] = (m[0x1D] ^ m[0x13]) & 0xFF

        for i in range(MASK_SIZE):
            self.video_mask2[i] = (m[i] ^ 0xFF) & 0xFF

    def _decrypt_video(self, data: bytearray) -> None:
        """Decrypt video chunk in-place."""
        size = len(data) - VIDEO_OFFSET
        if size < MIN_VIDEO_SIZE:
            return

        mask = bytearray(self.video_mask2)

        for i in range(0x100, size):
            idx = i & 0x1F
            pos = i + VIDEO_OFFSET
            data[pos] ^= mask[idx]
            mask[idx] = (data[pos] ^ self.video_mask2[idx]) & 0xFF

        mask[:MASK_SIZE] = self.video_mask1[:MASK_SIZE]
        for i in range(0x100):
            idx = i & 0x1F
            pos = i + VIDEO_OFFSET
            pos2 = 0x100 + i + VIDEO_OFFSET
            mask[idx] ^= data[pos2]
            data[pos] ^= mask[idx]

    def _open_stream(
        self, file_path: Path, streams: dict, paths: dict, stream_type: str
    ) -> BinaryIO:
        if file_path not in streams:
            streams[file_path] = open(file_path, "wb")
            paths.setdefault(stream_type, []).append(file_path)
        return streams[file_path]

    def _process_chunk(
        self,
        header: ChunkHeader,
        data: bytearray,
        output_path: Path,
        base_name: str,
        streams: dict,
        file_paths: dict,
    ) -> None:
        if header.signature == SIG_VIDEO and header.data_type == 0:
            self._decrypt_video(data)
            file_path = output_path / f"{base_name}.ivf"
            stream = self._open_stream(file_path, streams, file_paths, "ivf")
            stream.write(data)

        elif header.signature == SIG_AUDIO and header.data_type == 0:
            file_path = output_path / f"{base_name}_{header.channel_no}.hca"
            stream = self._open_stream(file_path, streams, file_paths, "hca")
            stream.write(data)

        elif header.signature not in (
            SIG_CRID,
            SIG_VIDEO,  # (non-zero data_type like metadata)
            SIG_AUDIO,  # (non-zero data_type)
            SIG_CUE,
            0x40415050,  # @APP
            0x40414C50,  # @ALP
            0x40534254,  # @SBT
        ):
            log.warning(f"Unknown signature {header.signature}")

    def demux(self, output_path: Path) -> dict[str, list[Path]]:
        base_name = self.file_path.stem
        streams = {}
        file_paths = {}
        file_size = self.file_path.stat().st_size

        with (
            open(self.file_path, "rb") as fp,
            tqdm(
                total=file_size,
                desc="Demuxing USM",
                unit="B",
                unit_scale=True,
                leave=False,
                dynamic_ncols=True,
            ) as pbar,
        ):
            while True:
                header_data = fp.read(HEADER_SIZE)
                if len(header_data) < HEADER_SIZE:
                    pbar.update(len(header_data))
                    break

                header = ChunkHeader.from_bytes(header_data)
                pbar.update(header.data_size + HEADER_SIZE - 0x18)

                data_size = header.data_size - header.data_offset - header.padding_size
                fp.seek(header.data_offset - 0x18, 1)
                data = bytearray(fp.read(data_size))
                fp.seek(header.padding_size, 1)

                self._process_chunk(
                    header,
                    data,
                    output_path,
                    base_name,
                    streams,
                    file_paths,
                )

        for stream in streams.values():
            stream.close()

        return file_paths
