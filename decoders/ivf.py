import re
import struct
import subprocess

from pathlib import Path
from typing import BinaryIO

import typer
from tqdm import tqdm


# IVF format constants
IVF_SIGNATURE = 0x46494B44  # "DKIF" in little-endian
HEADER_MIN_SIZE = 28
FRAME_HEADER_SIZE = 12


def parse_vp9_dimensions(frame_data: bytes) -> tuple[int, int] | None:
    """Parse VP9 frame to extract width and height.

    Args:
        frame_data: Raw VP9 frame data

    Returns:
        Tuple of (width, height) or None if parsing fails
    """
    if len(frame_data) < 10:
        return None

    try:
        # VP9 frame marker (bits 0-1 should be 0b10)
        frame_marker = (frame_data[0] >> 6) & 0x3
        if frame_marker != 0b10:
            return None

        # Profile (bits 2-3)
        profile = (frame_data[0] >> 4) & 0x3

        # Show existing frame bit (bit 4)
        show_existing_frame = (frame_data[0] >> 3) & 0x1
        if show_existing_frame:
            return None  # Cannot parse dimensions from reference frame

        # Frame type (bit 5): 0=key frame, 1=non-key frame
        frame_type = (frame_data[0] >> 2) & 0x1

        if frame_type == 0:  # Key frame
            # Parse sync code (bytes 1-3 should be 0x498342)
            sync_code = (frame_data[1] << 16) | (frame_data[2] << 8) | frame_data[3]
            if sync_code != 0x498342:
                return None

            # Color config at byte 4
            byte_idx = 4
            bit_idx = 0

            # Color space (3 bits) and color range (1 bit)
            byte_idx += 1

            # Parse width and height (16 bits each in little-endian)
            if len(frame_data) < byte_idx + 4:
                return None

            width = ((frame_data[byte_idx + 1] & 0xFF) << 8) | (frame_data[byte_idx] & 0xFF)
            height = ((frame_data[byte_idx + 3] & 0xFF) << 8) | (frame_data[byte_idx + 2] & 0xFF)

            # Add 1 to get actual dimensions
            return (width + 1, height + 1)
    except (IndexError, struct.error):
        return None

    return None


class IVFHeader:
    """IVF file header structure for reading and writing."""

    __slots__ = (
        "codec",
        "framerate",
        "frames",
        "header_length",
        "height",
        "timescale",
        "version",
        "width",
    )

    def __init__(self, width: int = 0, height: int = 0, framerate: int = 0, timescale: int = 0):
        """Initialize IVF header.

        Args:
            width: Video width in pixels
            height: Video height in pixels
            framerate: Frame rate numerator
            timescale: Frame rate denominator
        """
        self.version = 0
        self.header_length = 32
        self.codec = "VP90"
        self.width = width
        self.height = height
        self.framerate = framerate
        self.timescale = timescale
        self.frames = 0

    @classmethod
    def from_file(cls, fp: BinaryIO) -> "IVFHeader":
        """Parse IVF header from file."""
        header = cls()

        # Verify signature
        signature = struct.unpack("<I", fp.read(4))[0]
        if signature != IVF_SIGNATURE:
            raise ValueError(f"Invalid IVF file: wrong signature 0x{signature:08X}")

        # Read header fields
        header.version = struct.unpack("<H", fp.read(2))[0]
        header.header_length = struct.unpack("<H", fp.read(2))[0]
        header.codec = fp.read(4).decode("ascii")
        header.width = struct.unpack("<H", fp.read(2))[0]
        header.height = struct.unpack("<H", fp.read(2))[0]
        header.framerate = struct.unpack("<I", fp.read(4))[0]
        header.timescale = struct.unpack("<I", fp.read(4))[0]
        header.frames = struct.unpack("<I", fp.read(4))[0]

        # Skip unused padding bytes
        fp.read(header.header_length - HEADER_MIN_SIZE)
        return header

    def to_bytes(self) -> bytes:
        """Serialize header to bytes for writing."""
        data = bytearray()
        data.extend(struct.pack("<I", IVF_SIGNATURE))  # Signature "DKIF"
        data.extend(struct.pack("<H", self.version))  # Version
        data.extend(struct.pack("<H", self.header_length))  # Header length
        data.extend(self.codec.encode("ascii"))  # Codec
        data.extend(struct.pack("<H", self.width))  # Width
        data.extend(struct.pack("<H", self.height))  # Height
        data.extend(struct.pack("<I", self.framerate))  # Framerate
        data.extend(struct.pack("<I", self.timescale))  # Timescale
        data.extend(struct.pack("<I", self.frames))  # Frame count
        data.extend(b"\x00" * 4)  # Unused padding
        return bytes(data)


class IVFWriter:
    """Write IVF format files with proper headers."""

    def __init__(self, file_path: Path, width: int, height: int, framerate: int = 30000, timescale: int = 1000):
        """Initialize IVF writer.

        Args:
            file_path: Output file path
            width: Video width in pixels
            height: Video height in pixels
            framerate: Frame rate numerator (default: 30000)
            timescale: Frame rate denominator (default: 1000)
        """
        self.file_path = file_path
        self.header = IVFHeader(width, height, framerate, timescale)
        self.fp: BinaryIO | None = None

    def __enter__(self):
        """Context manager entry."""
        self.fp = open(self.file_path, "wb")
        # Write placeholder header (will be updated with final frame count)
        self._write_header()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - update header with final frame count."""
        if self.fp:
            # Update frame count in header and rewrite it
            self.header.frames = self.header.frames  # Already incremented during writes
            self.fp.seek(0)
            self.fp.write(self.header.to_bytes())
            self.fp.close()
            self.fp = None

    def _write_header(self) -> None:
        """Write IVF file header."""
        if not self.fp:
            raise RuntimeError("File not open")
        self.fp.write(self.header.to_bytes())

    def write_frame(self, frame_data: bytes, timestamp: int = 0) -> None:
        """Write a video frame with IVF frame header.

        Args:
            frame_data: Raw VP9 frame data
            timestamp: Frame timestamp (will auto-increment if 0)
        """
        if not self.fp:
            raise RuntimeError("File not open")

        # Calculate timestamp if not provided
        if timestamp == 0:
            timestamp = self.header.frames

        # Write frame header (12 bytes)
        self.fp.write(struct.pack("<I", len(frame_data)))  # Frame size
        self.fp.write(struct.pack("<Q", timestamp))  # Timestamp

        # Write frame data
        self.fp.write(frame_data)
        self.header.frames += 1


class IVF:
    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self.filename = self.file_path.name
        self.header: IVFHeader = self._parse_header()
        self.fps = self.header.framerate / self.header.timescale
        self.duration_ms = (self.header.frames / self.fps) * 1000
        self.frame_duration_ms = (1.0 / self.fps) * 1000

    def _parse_header(self) -> IVFHeader:
        """Parse IVF file header."""
        with open(self.file_path, "rb") as fp:
            return IVFHeader.from_file(fp)

    def convert_to_mkv(self, output_path: Path, subtitle_files: list[Path] = None, font_files: list[Path] = None) -> str:
        """Convert IVF to MKV using ffmpeg.

            Legacy, just keeping it for future references. We're using VapourSynth to process.
        """
        mkv_file = output_path / f"{Path(self.filename).stem}.mkv"
        typer.echo(f"Converting {self.filename} to MKV...")

        # Build ffmpeg command
        x265_params = [
            "profile=main10",
            "cutree=0",
            "deblock=-2,-2",
            "no-sao=1",
            "tskip=1",
            "cbqpoffs=-2",
            "qcomp=0.7",
            "lookahead-slices=0",
            "keyint=300",
            "min-keyint=30",
            "max-merge=5",
            "ref=6",
            "bframes=12",
            "rd=4",
            "psy-rd=1.5",
            "psy-rdoq=1.5",
            "aq-mode=3",
            "aq-strength=0.8",
            "colorprim=1",
            "colormatrix=1",
            "transfer=1",
        ]
        x265_params = ":".join(x265_params)
        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output file
            "-progress", "pipe:1",  # Output progress to stdout
            "-loglevel", "error",  # Only show errors on stderr
            "-i",
            str(self.file_path),
            "-c:v", "libx265",
            "-pix_fmt", "yuv420p10le",
            "-vf", "zscale=matrix=bt709,format=yuv420p10le",
            "-preset", "slower",
            "-crf", "12",
            "-x265-params",
            x265_params,
            str(mkv_file),
        ]

        typer.echo(f"Command: {"".join(cmd)}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )

            # Create progress bar
            total_frames = self.header.frames
            pbar = tqdm(total=total_frames, unit=" frames", desc="Processing")

            # Parse progress output
            frame_pattern = re.compile(r"frame=\s*(\d+)")
            for line in process.stdout:
                match = frame_pattern.search(line)
                if match:
                    current_frame = int(match.group(1))
                    pbar.n = current_frame
                    pbar.refresh()

            pbar.close()

            # Wait for process to complete
            return_code = process.wait()
            if return_code != 0:
                stderr_output = process.stderr.read()
                typer.echo(f"Error converting video: ffmpeg exited with code {return_code}")
                if stderr_output:
                    typer.echo(f"{stderr_output}")
                raise typer.Exit(1)

            return str(mkv_file)
        except FileNotFoundError:
            typer.echo(
                "ffmpeg not found. Place ffmpeg in the root directory and try again."
            )
            raise typer.Exit(1) from None
