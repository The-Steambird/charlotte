import struct
import subprocess

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import typer

from decoders.channel import DecodeTables
from utils.logger import log


# HCA format constants
HCA_SAMPLES_PER_BLOCK = 0x80  # 128 samples per block
HCA_SUBFRAMES = 8  # Number of subframes per block
IMDCT_ITERATIONS = 7  # Number of IMDCT butterfly/rotation iterations
WINDOW_SIZE = HCA_SAMPLES_PER_BLOCK // 2  # 64 samples per window half


@dataclass
class HCAHeader:
    """HCA file header information."""

    version: int = 0
    data_offset: int = 0
    channel_count: int = 0
    sampling_rate: int = 0
    block_count: int = 0
    block_size: int = 0
    comp_r01: int = 0
    comp_r02: int = 0
    comp_r03: int = 0
    comp_r04: int = 0
    comp_r05: int = 0
    comp_r06: int = 0
    comp_r07: int = 0
    comp_r08: int = 0
    comp_r09: int = 0
    ath_type: int = 0
    loop_flag: bool = False
    ciph_type: int = 0
    volume: float = 1.0


class ClData:
    """Bit manipulation class for HCA decoding."""

    MASK = [0xFFFFFF, 0x7FFFFF, 0x3FFFFF, 0x1FFFFF, 0x0FFFFF, 0x07FFFF, 0x03FFFF, 0x01FFFF]  # fmt: skip

    def __init__(self, data: bytes, size: int):
        self.data = data
        self.size = size * 8 - 16
        self.bit = 0

    def check_bit(self, bit_size: int) -> int:
        """Check bit value without advancing position."""
        if self.bit + bit_size > self.size:
            return 0

        data_offset = self.bit >> 3
        v = self.data[data_offset]
        v = v << 8 | (self.data[data_offset + 1] if data_offset + 1 < len(self.data) else 0)
        v = v << 8 | (self.data[data_offset + 2] if data_offset + 2 < len(self.data) else 0)
        v &= self.MASK[self.bit & 7]
        v >>= 24 - (self.bit & 7) - bit_size
        return v

    def get_bit(self, bit_size: int) -> int:
        """Get bit value and advance position."""
        v = self.check_bit(bit_size)
        self.bit += bit_size
        return v

    def add_bit(self, bit_size: int) -> None:
        """Advance bit position."""
        self.bit += bit_size


class Channel:
    """Audio channel decoder."""

    def __init__(self):
        # Spectral coefficients (frequency domain data)
        self.spectral_coeffs = np.zeros(HCA_SAMPLES_PER_BLOCK, dtype=np.float32)
        self.dequant_scale_table = np.zeros(HCA_SAMPLES_PER_BLOCK, dtype=np.float32)
        self.quantized_values = np.zeros(HCA_SAMPLES_PER_BLOCK, dtype=np.uint8)
        self.scale_factors = np.zeros(HCA_SAMPLES_PER_BLOCK, dtype=np.uint8)
        self.stereo_scale_indices = np.zeros(HCA_SUBFRAMES, dtype=np.int8)

        # Channel metadata
        self.channel_type = 0  # 0=unused, 1=main/mid channel, 2=side channel
        self.intensity_stereo_offset = 0
        self.active_coeffs_count = 0

        # IMDCT working buffers
        self.imdct_buffer1 = np.zeros(HCA_SAMPLES_PER_BLOCK, dtype=np.float32)
        self.imdct_buffer2 = np.zeros(HCA_SAMPLES_PER_BLOCK, dtype=np.float32)
        self.overlap_buffer = np.zeros(HCA_SAMPLES_PER_BLOCK, dtype=np.float32)

        # Output samples (8 subframes x 128 samples each)
        self.output_samples = [
            np.zeros(HCA_SAMPLES_PER_BLOCK, dtype=np.float32) for _ in range(HCA_SUBFRAMES)
        ]

    def _decode_values(self, data: ClData, mode: int) -> None:
        """Decode quantization values from bitstream."""
        if mode >= 6:
            # Absolute encoding: each value is encoded independently
            for i in range(self.active_coeffs_count):
                self.quantized_values[i] = data.get_bit(6)
        elif mode != 0:
            # Delta encoding: values are encoded as differences from previous value
            current_val = data.get_bit(6)
            max_delta = (1 << mode) - 1
            delta_offset = max_delta >> 1
            self.quantized_values[0] = current_val

            for i in range(1, self.active_coeffs_count):
                delta = data.get_bit(mode)
                current_val = (
                    data.get_bit(6) if delta == max_delta else current_val + delta - delta_offset
                )
                self.quantized_values[i] = current_val
        else:
            # All values are zero
            self.quantized_values[:] = 0

    def _compute_scale_factors(self, resolution_index: int, ath_table: bytes) -> None:
        """Compute scale factors using ATH (Absolute Threshold of Hearing).

        Args:
            resolution_index: Resolution/quality parameter for the block
            ath_table: Absolute Threshold of Hearing lookup table
        """
        for i in range(self.active_coeffs_count):
            scale_val = int(self.quantized_values[i])
            if scale_val != 0:
                # Compute scale factor based on ATH, resolution, and quantized value
                scale_val = int(ath_table[i]) + (resolution_index + i >> 8) - scale_val * 5 // 2 + 1
                # Clamp and map to scale factor lookup table
                scale_val = (
                    15
                    if scale_val < 0
                    else (1 if scale_val >= 0x39 else DecodeTables.DECODE1_SCALELIST[scale_val])
                )
            self.scale_factors[i] = scale_val
        # Clear unused scale factors
        self.scale_factors[self.active_coeffs_count :] = 0

    def _compute_base_table(self) -> None:
        """Compute dequantization scale table (base magnitude for each coefficient)."""

        for i in range(self.active_coeffs_count):
            value_coef = (
                DecodeTables.DECODE1_VALUE[self.quantized_values[i]]
                if 0 <= self.quantized_values[i] < 64
                else 0.0
            )
            self.dequant_scale_table[i] = (
                value_coef * DecodeTables.DECODE1_SCALE[self.scale_factors[i]]
            )

    def decode1(
        self,
        data: ClData,
        intensity_count: int,
        resolution_index: int,
        ath_table: bytes,
    ) -> None:
        """Decode step 1 - extract quantization values and scales from bitstream.

        Args:
            data: Bitstream reader
            intensity_count: Number of intensity stereo bands
            resolution_index: Resolution/quality parameter
            ath_table: Absolute Threshold of Hearing lookup table
        """
        mode = data.get_bit(3)
        self._decode_values(data, mode)

        # Handle channel-specific values
        if self.channel_type == 2:
            # Side channel: read stereo scale indices
            val = data.check_bit(4)
            self.stereo_scale_indices[0] = val
            if val < 15:
                for i in range(len(self.stereo_scale_indices)):
                    self.stereo_scale_indices[i] = data.get_bit(4)
        else:
            # Main/mid channel: read intensity stereo values
            for i in range(intensity_count):
                self.quantized_values[self.intensity_stereo_offset + i] = data.get_bit(6)

        self._compute_scale_factors(resolution_index, ath_table)
        self._compute_base_table()

    def decode2(self, data: ClData) -> None:
        """Decode step 2 - dequantize spectral coefficients.

        Reads quantized coefficients from bitstream and applies dequantization
        to produce frequency-domain spectral values.
        """

        for i in range(self.active_coeffs_count):
            scale = self.scale_factors[i]
            bit_size = DecodeTables.DECODE2_LIST1[scale]
            raw_val = data.get_bit(bit_size)

            if scale < 8:
                # Low scale: use lookup table for small quantized values
                idx = raw_val + (scale << 4)
                data.add_bit(DecodeTables.DECODE2_LIST2[idx] - bit_size)
                coefficient = DecodeTables.DECODE2_LIST3[idx]
            else:
                # High scale: decode sign and magnitude directly
                # Format: LSB is sign bit, remaining bits are magnitude
                coefficient = float((1 - ((raw_val & 1) << 1)) * (raw_val >> 1))
                if coefficient == 0:
                    data.add_bit(-1)

            self.spectral_coeffs[i] = self.dequant_scale_table[i] * coefficient

        # Clear unused coefficients
        self.spectral_coeffs[self.active_coeffs_count :] = 0

    def decode3(
        self,
        intensity_bands: int,
        band_width: int,
        base_band_idx: int,
        total_bands: int,
    ) -> None:
        """Decode step 3 - intensity stereo processing.

        Reconstructs high-frequency bands from lower-frequency bands using
        intensity stereo coding, which saves bits by encoding only magnitude
        differences for high frequencies.

        Args:
            intensity_bands: Number of intensity stereo bands to process
            band_width: Width of each band (number of coefficients)
            base_band_idx: Index of the base band to copy from
            total_bands: Total number of frequency bands
        """
        if self.channel_type == 2 or band_width <= 0:
            return

        for band in range(intensity_bands):
            for j in range(band_width):
                dest_idx = base_band_idx + j
                source_idx = base_band_idx - 1 - j
                if dest_idx >= total_bands:
                    break
                # Calculate intensity ratio from stored values
                intensity_diff = int(
                    self.quantized_values[self.intensity_stereo_offset + band]
                ) - int(self.quantized_values[source_idx])
                # Apply intensity scaling to reconstruct high-frequency coefficient
                self.spectral_coeffs[dest_idx] = (
                    DecodeTables.DECODE3_LIST[intensity_diff] * self.spectral_coeffs[source_idx]
                )

        self.spectral_coeffs[HCA_SAMPLES_PER_BLOCK - 1] = 0

    def decode4(
        self,
        subframe_idx: int,
        coeff_count: int,
        start_idx: int,
        stereo_mode: int,
        side_channel: "Channel",
    ) -> None:
        """Decode step 4 - mid/side stereo decoding.

        Converts mid/side stereo encoding to left/right stereo channels.
        Mid/side stereo saves bits by encoding (L+R) and (L-R) instead of L and R.

        Args:
            subframe_idx: Current subframe index (0-7)
            coeff_count: Number of coefficients to process
            start_idx: Starting index in spectral coefficient array
            stereo_mode: Stereo mode (0 = disabled, >0 = enabled)
            side_channel: The side channel to decode into
        """
        if self.channel_type != 1 or stereo_mode == 0:
            return

        # Get scaling factors for mid/side conversion
        mid_scale = DecodeTables.DECODE4_LIST[side_channel.stereo_scale_indices[subframe_idx]]
        side_scale = mid_scale - 2.0

        # Convert mid/side to left/right stereo
        # Left = Mid * mid_scale, Right = Mid * side_scale
        for i in range(coeff_count):
            idx = start_idx + i
            side_channel.spectral_coeffs[idx] = self.spectral_coeffs[idx] * side_scale
            self.spectral_coeffs[idx] = self.spectral_coeffs[idx] * mid_scale

    def decode5(self, subframe_idx: int) -> None:
        """Decode step 5 - IMDCT (Inverse Modified Discrete Cosine Transform).

        Converts frequency domain coefficients to time domain samples using a
        two-stage process: butterfly operations followed by rotation operations,
        then applies windowing and overlap-add for smooth transitions.

        Args:
            subframe_idx: Index of subframe to store output (0-7)
        """
        # Stage 1: Butterfly operations (FFT-like transform)
        source_buf, dest_buf = self.spectral_coeffs, self.imdct_buffer1

        for iteration in range(IMDCT_ITERATIONS):
            num_groups = 1 << iteration
            group_size = HCA_SAMPLES_PER_BLOCK // 2 >> iteration
            src_idx, dest1_idx, dest2_idx = 0, 0, group_size

            for _ in range(num_groups):
                for _ in range(group_size):
                    a, b = source_buf[src_idx], source_buf[src_idx + 1]
                    dest_buf[dest1_idx], dest_buf[dest2_idx] = b + a, a - b
                    src_idx += 2
                    dest1_idx += 1
                    dest2_idx += 1
                dest1_idx += group_size
                dest2_idx += group_size

            # Swap buffers for next iteration
            source_buf, dest_buf = dest_buf, source_buf

        # Stage 2: Rotation operations with twiddle factors (DCT transform)
        source_buf, dest_buf = self.imdct_buffer1, self.spectral_coeffs

        for iteration in range(IMDCT_ITERATIONS):
            num_groups = HCA_SAMPLES_PER_BLOCK // 2 >> iteration
            group_size = 1 << iteration
            pair1_idx, pair2_idx = 0, group_size
            dest1_idx, dest2_idx = 0, group_size * 2 - 1
            twiddle_idx = 0

            for _ in range(num_groups):
                for _ in range(group_size):
                    a, b = source_buf[pair1_idx], source_buf[pair2_idx]
                    cos_coef = DecodeTables.DECODE5_LIST1[iteration][twiddle_idx]
                    sin_coef = DecodeTables.DECODE5_LIST2[iteration][twiddle_idx]
                    dest_buf[dest1_idx] = a * cos_coef - b * sin_coef
                    dest_buf[dest2_idx] = a * sin_coef + b * cos_coef
                    twiddle_idx += 1
                    pair1_idx += 1
                    pair2_idx += 1
                    dest1_idx += 1
                    dest2_idx -= 1

                pair1_idx += group_size
                pair2_idx += group_size
                dest1_idx += group_size
                dest2_idx += group_size * 3

            # Swap buffers for next iteration
            source_buf, dest_buf = dest_buf, source_buf

        # Copy result to intermediate buffer for windowing
        self.imdct_buffer2[:] = source_buf[:]

        # Stage 3: Windowing and overlap-add
        output = self.output_samples[subframe_idx]
        ascending_window = DecodeTables.DECODE5_LIST3[0]
        descending_window = DecodeTables.DECODE5_LIST3[1]

        # First half of output: apply ascending window and add overlap from previous block
        imdct_idx = WINDOW_SIZE  # Start from middle of IMDCT output
        overlap_idx = 0
        out_idx = 0
        for i in range(WINDOW_SIZE):
            output[out_idx] = (
                self.imdct_buffer2[imdct_idx] * ascending_window[i]
                + self.overlap_buffer[overlap_idx]
            )
            out_idx += 1
            imdct_idx += 1
            overlap_idx += 1

        # Second half: apply descending window and subtract overlap
        window_idx = 0
        for i in range(WINDOW_SIZE):
            imdct_idx -= 1
            output[out_idx] = (
                descending_window[window_idx] * self.imdct_buffer2[imdct_idx]
                - self.overlap_buffer[overlap_idx]
            )
            out_idx += 1
            window_idx += 1
            overlap_idx += 1

        # Store overlap for next block (first part: windowed second half)
        window_idx = WINDOW_SIZE
        imdct_idx = WINDOW_SIZE - 1
        overlap_idx = 0
        for i in range(WINDOW_SIZE):
            window_idx -= 1
            self.overlap_buffer[overlap_idx] = (
                self.imdct_buffer2[imdct_idx] * descending_window[window_idx]
            )
            imdct_idx -= 1
            overlap_idx += 1

        # Store overlap for next block (second part: windowed first half)
        window_idx = WINDOW_SIZE
        for i in range(WINDOW_SIZE):
            imdct_idx += 1
            window_idx -= 1
            self.overlap_buffer[overlap_idx] = (
                ascending_window[window_idx] * self.imdct_buffer2[imdct_idx]
            )
            overlap_idx += 1


class HCA:
    """HCA audio decoder.

    Decodes CRI Middleware's HCA (High Compression Audio) format to PCM audio.
    The decoder handles encrypted files and outputs 16-bit PCM WAV files.

    Args:
        file_path: Path to the HCA file
        key1: Optional decryption key (4 bytes) for cipher type 56
        key2: Optional decryption key (2 bytes) for cipher type 56
    """

    def __init__(self, file_path: Path, key1: int | None = None, key2: int | None = None):
        self.file_path = Path(file_path)
        self.key1 = key1 or bytes(4)
        self.key2 = key2 or bytes(4)
        self.ciph_table = bytearray(0x100)
        self.ath_table = bytearray(0x80)
        self.encrypted = False
        self.header_struct = HCAHeader()
        self.channels: list[Channel] = []
        self.header_bytes = bytearray()
        self.data = bytearray()

        self._read_header()

    @staticmethod
    def _ceil2(a: int, b: int) -> int:
        """Calculate ceiling of a divided by 2^b.

        Args:
           a: The dividend
           b: The power of 2 for the divisor (2^b)

        Returns:
           ceil(a / 2^b)
        """
        if b == 0:
            return 0
        return (a + (1 << b) - 1) >> b

    def _init56_create_table(self, key: int) -> bytearray:
        """Generate a substitution table for cipher type 56.

        Args:
            key: Input key byte for table generation.

        Returns:
            A 16-byte substitution table.
        """
        table = bytearray(0x10)
        mul = (key & 1) << 3 | 5
        add = key & 0xE | 1
        key >>= 4

        for i in range(0x10):
            key = (key * mul + add) & 0xF
            table[i] = key

        return table

    def _init_mask(self, mask_type: int) -> None:
        """Initialize the cipher mask table based on the cipher type.

        Args:
            mask_type: The cipher type identifier (0 = none, 1 = constant, 56 = key-based).
        """
        if mask_type == 0:
            for i in range(0x100):
                self.ciph_table[i] = i

        elif mask_type == 1:
            v = 0
            for i in range(0xFF):
                v = (v * 13 + 11) & 0xFF
                if v == 0 or v == 0xFF:
                    v = (v * 13 + 11) & 0xFF
                self.ciph_table[i] = v
            self.ciph_table[0] = 0
            self.ciph_table[0xFF] = 0xFF

        elif mask_type == 56:
            t1 = bytearray(8)
            key1 = struct.unpack("<I", self.key1)[0]
            key2 = struct.unpack("<I", self.key2)[0]

            if key1 == 0:
                key2 = (key2 - 1) & 0xFFFFFFFF
            key1 = (key1 - 1) & 0xFFFFFFFF

            for i in range(7):
                t1[i] = key1 & 0xFF
                key1 = ((key1 >> 8) | (key2 << 24)) & 0xFFFFFFFF
                key2 >>= 8

            t2 = bytearray(
                [
                    t1[1],
                    t1[1] ^ t1[6],
                    t1[2] ^ t1[3],
                    t1[2],
                    t1[2] ^ t1[1],
                    t1[3] ^ t1[4],
                    t1[3],
                    t1[3] ^ t1[2],
                    t1[4] ^ t1[5],
                    t1[4],
                    t1[4] ^ t1[3],
                    t1[5] ^ t1[6],
                    t1[5],
                    t1[5] ^ t1[4],
                    t1[6] ^ t1[1],
                    t1[6],
                ]
            )

            t3 = bytearray(0x100)
            t31 = self._init56_create_table(t1[0])

            for i in range(0x10):
                t32 = self._init56_create_table(t2[i])
                v = t31[i] << 4
                for j, val in enumerate(t32):
                    t3[i * 0x10 + j] = v | val

            i_table = 1
            v = 0
            for _ in range(0x100):
                v = (v + 0x11) & 0xFF
                a = t3[v]
                if a != 0 and a != 0xFF:
                    self.ciph_table[i_table] = a
                    i_table += 1

            self.ciph_table[0] = 0
            self.ciph_table[0xFF] = 0xFF

    def _mask(self, data: bytearray) -> None:
        """Apply cipher mask to data."""
        for i in range(len(data)):
            data[i] = self.ciph_table[data[i]]

    @staticmethod
    def _checksum(data: bytes) -> int:
        """Calculate 16-bit checksum for HCA blocks.

        Args:
            data: The data bytes to checksum (excluding the checksum field itself).

        Returns:
            The calculated 16-bit checksum.
        """
        v = [
            0x0000, 0x8005, 0x800F, 0x000A, 0x801B, 0x001E, 0x0014, 0x8011, 0x8033, 0x0036, 0x003C,
            0x8039, 0x0028, 0x802D, 0x8027, 0x0022, 0x8063, 0x0066, 0x006C, 0x8069, 0x0078, 0x807D,
            0x8077, 0x0072, 0x0050, 0x8055, 0x805F, 0x005A, 0x804B, 0x004E, 0x0044, 0x8041, 0x80C3,
            0x00C6, 0x00CC, 0x80C9, 0x00D8, 0x80DD, 0x80D7, 0x00D2, 0x00F0, 0x80F5, 0x80FF, 0x00FA,
            0x80EB, 0x00EE, 0x00E4, 0x80E1, 0x00A0, 0x80A5, 0x80AF, 0x00AA, 0x80BB, 0x00BE, 0x00B4,
            0x80B1, 0x8093, 0x0096, 0x009C, 0x8099, 0x0088, 0x808D, 0x8087, 0x0082, 0x8183, 0x0186,
            0x018C, 0x8189, 0x0198, 0x819D, 0x8197, 0x0192, 0x01B0, 0x81B5, 0x81BF, 0x01BA, 0x81AB,
            0x01AE, 0x01A4, 0x81A1, 0x01E0, 0x81E5, 0x81EF, 0x01EA, 0x81FB, 0x01FE, 0x01F4, 0x81F1,
            0x81D3, 0x01D6, 0x01DC, 0x81D9, 0x01C8, 0x81CD, 0x81C7, 0x01C2, 0x0140, 0x8145, 0x814F,
            0x014A, 0x815B, 0x015E, 0x0154, 0x8151, 0x8173, 0x0176, 0x017C, 0x8179, 0x0168, 0x816D,
            0x8167, 0x0162, 0x8123, 0x0126, 0x012C, 0x8129, 0x0138, 0x813D, 0x8137, 0x0132, 0x0110,
            0x8115, 0x811F, 0x011A, 0x810B, 0x010E, 0x0104, 0x8101, 0x8303, 0x0306, 0x030C, 0x8309,
            0x0318, 0x831D, 0x8317, 0x0312, 0x0330, 0x8335, 0x833F, 0x033A, 0x832B, 0x032E, 0x0324,
            0x8321, 0x0360, 0x8365, 0x836F, 0x036A, 0x837B, 0x037E, 0x0374, 0x8371, 0x8353, 0x0356,
            0x035C, 0x8359, 0x0348, 0x834D, 0x8347, 0x0342, 0x03C0, 0x83C5, 0x83CF, 0x03CA, 0x83DB,
            0x03DE, 0x03D4, 0x83D1, 0x83F3, 0x03F6, 0x03FC, 0x83F9, 0x03E8, 0x83ED, 0x83E7, 0x03E2,
            0x83A3, 0x03A6, 0x03AC, 0x83A9, 0x03B8, 0x83BD, 0x83B7, 0x03B2, 0x0390, 0x8395, 0x839F,
            0x039A, 0x838B, 0x038E, 0x0384, 0x8381, 0x0280, 0x8285, 0x828F, 0x028A, 0x829B, 0x029E,
            0x0294, 0x8291, 0x82B3, 0x02B6, 0x02BC, 0x82B9, 0x02A8, 0x82AD, 0x82A7, 0x02A2, 0x82E3,
            0x02E6, 0x02EC, 0x82E9, 0x02F8, 0x82FD, 0x82F7, 0x02F2, 0x02D0, 0x82D5, 0x82DF, 0x02DA,
            0x82CB, 0x02CE, 0x02C4, 0x82C1, 0x8243, 0x0246, 0x024C, 0x8249, 0x0258, 0x825D, 0x8257,
            0x0252, 0x0270, 0x8275, 0x827F, 0x027A, 0x826B, 0x026E, 0x0264, 0x8261, 0x0220, 0x8225,
            0x822F, 0x022A, 0x823B, 0x023E, 0x0234, 0x8231, 0x8213, 0x0216, 0x021C, 0x8219, 0x0208,
            0x820D, 0x8207, 0x0202,
        ]  # fmt: skip

        checksum = 0
        for byte in data:
            checksum = ((checksum << 8) ^ v[(checksum >> 8) ^ byte]) & 0xFFFF
        return checksum

    def _ath_init(self) -> None:
        """Initialize the Absolute Threshold of Hearing (ATH) table.

        This table is used to scale quantization noise based on human hearing sensitivity.
        The table generation depends on the ATH type specified in the header.
        """
        if self.header_struct.ath_type == 0:
            self.ath_table = bytearray(0x80)

        elif self.header_struct.ath_type == 1:
            ath_list = bytes([
                0x78, 0x5F, 0x56, 0x51, 0x4E, 0x4C, 0x4B, 0x49, 0x48, 0x48, 0x47, 0x46, 0x46, 0x45, 0x45, 0x45,
                0x44, 0x44, 0x44, 0x44, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42,
                0x42, 0x42, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x40, 0x40, 0x40, 0x40,
                0x40, 0x40, 0x40, 0x40, 0x40, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F,
                0x3F, 0x3F, 0x3F, 0x3E, 0x3E, 0x3E, 0x3E, 0x3E, 0x3E, 0x3D, 0x3D, 0x3D, 0x3D, 0x3D, 0x3D, 0x3D,
                0x3C, 0x3C, 0x3C, 0x3C, 0x3C, 0x3C, 0x3C, 0x3C, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B,
                0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B,
                0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3B, 0x3C, 0x3C, 0x3C, 0x3C, 0x3C, 0x3C, 0x3C, 0x3C,
                0x3D, 0x3D, 0x3D, 0x3D, 0x3D, 0x3D, 0x3D, 0x3D, 0x3E, 0x3E, 0x3E, 0x3E, 0x3E, 0x3E, 0x3E, 0x3F,
                0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F, 0x3F,
                0x3F, 0x3F, 0x3F, 0x3F, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40,
                0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x40, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41,
                0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41,
                0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42,
                0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x43, 0x43, 0x43,
                0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x43, 0x44, 0x44,
                0x44, 0x44, 0x44, 0x44, 0x44, 0x44, 0x44, 0x44, 0x44, 0x44, 0x44, 0x44, 0x45, 0x45, 0x45, 0x45,
                0x45, 0x45, 0x45, 0x45, 0x45, 0x45, 0x45, 0x45, 0x46, 0x46, 0x46, 0x46, 0x46, 0x46, 0x46, 0x46,
                0x46, 0x46, 0x47, 0x47, 0x47, 0x47, 0x47, 0x47, 0x47, 0x47, 0x47, 0x47, 0x48, 0x48, 0x48, 0x48,
                0x48, 0x48, 0x48, 0x48, 0x49, 0x49, 0x49, 0x49, 0x49, 0x49, 0x49, 0x49, 0x4A, 0x4A, 0x4A, 0x4A,
                0x4A, 0x4A, 0x4A, 0x4A, 0x4B, 0x4B, 0x4B, 0x4B, 0x4B, 0x4B, 0x4B, 0x4C, 0x4C, 0x4C, 0x4C, 0x4C,
                0x4C, 0x4D, 0x4D, 0x4D, 0x4D, 0x4D, 0x4D, 0x4E, 0x4E, 0x4E, 0x4E, 0x4E, 0x4E, 0x4F, 0x4F, 0x4F,
                0x4F, 0x4F, 0x4F, 0x50, 0x50, 0x50, 0x50, 0x50, 0x51, 0x51, 0x51, 0x51, 0x51, 0x52, 0x52, 0x52,
                0x52, 0x52, 0x53, 0x53, 0x53, 0x53, 0x54, 0x54, 0x54, 0x54, 0x54, 0x55, 0x55, 0x55, 0x55, 0x56,
                0x56, 0x56, 0x56, 0x57, 0x57, 0x57, 0x57, 0x57, 0x58, 0x58, 0x58, 0x59, 0x59, 0x59, 0x59, 0x5A,
                0x5A, 0x5A, 0x5A, 0x5B, 0x5B, 0x5B, 0x5B, 0x5C, 0x5C, 0x5C, 0x5D, 0x5D, 0x5D, 0x5D, 0x5E, 0x5E,
                0x5E, 0x5F, 0x5F, 0x5F, 0x60, 0x60, 0x60, 0x61, 0x61, 0x61, 0x61, 0x62, 0x62, 0x62, 0x63, 0x63,
                0x63, 0x64, 0x64, 0x64, 0x65, 0x65, 0x66, 0x66, 0x66, 0x67, 0x67, 0x67, 0x68, 0x68, 0x68, 0x69,
                0x69, 0x6A, 0x6A, 0x6A, 0x6B, 0x6B, 0x6B, 0x6C, 0x6C, 0x6D, 0x6D, 0x6D, 0x6E, 0x6E, 0x6F, 0x6F,
                0x70, 0x70, 0x70, 0x71, 0x71, 0x72, 0x72, 0x73, 0x73, 0x73, 0x74, 0x74, 0x75, 0x75, 0x76, 0x76,
                0x77, 0x77, 0x78, 0x78, 0x78, 0x79, 0x79, 0x7A, 0x7A, 0x7B, 0x7B, 0x7C, 0x7C, 0x7D, 0x7D, 0x7E,
                0x7E, 0x7F, 0x7F, 0x80, 0x80, 0x81, 0x81, 0x82, 0x83, 0x83, 0x84, 0x84, 0x85, 0x85, 0x86, 0x86,
                0x87, 0x88, 0x88, 0x89, 0x89, 0x8A, 0x8A, 0x8B, 0x8C, 0x8C, 0x8D, 0x8D, 0x8E, 0x8F, 0x8F, 0x90,
                0x90, 0x91, 0x92, 0x92, 0x93, 0x94, 0x94, 0x95, 0x95, 0x96, 0x97, 0x97, 0x98, 0x99, 0x99, 0x9A,
                0x9B, 0x9B, 0x9C, 0x9D, 0x9D, 0x9E, 0x9F, 0xA0, 0xA0, 0xA1, 0xA2, 0xA2, 0xA3, 0xA4, 0xA5, 0xA5,
                0xA6, 0xA7, 0xA7, 0xA8, 0xA9, 0xAA, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAE, 0xAF, 0xB0, 0xB1, 0xB1,
                0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF,
                0xC0, 0xC1, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD,
                0xCE, 0xCF, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xDB, 0xDC, 0xDD,
                0xDE, 0xDF, 0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xED, 0xEE,
                0xEF, 0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF7, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFF, 0xFF,
            ])  # fmt: skip

            v = 0
            for i in range(0x80):
                index = v >> 13
                if index >= 0x28E:
                    self.ath_table[i:] = [0xFF] * (0x80 - i)
                    break
                self.ath_table[i] = ath_list[index]
                v += self.header_struct.sampling_rate

    def _channel_init(self) -> None:
        """Initialize audio channels."""
        self.channels = [Channel() for _ in range(self.header_struct.channel_count)]

        if not (self.header_struct.comp_r01 == 1 and self.header_struct.comp_r02 == 15):
            raise ValueError("Invalid comp values")

        self.header_struct.comp_r09 = self._ceil2(
            self.header_struct.comp_r05
            - (self.header_struct.comp_r06 + self.header_struct.comp_r07),
            self.header_struct.comp_r08,
        )

        r = [0] * 0x10
        b = self.header_struct.channel_count // self.header_struct.comp_r03

        if self.header_struct.comp_r07 != 0 and b > 1:
            c = 0
            for _ in range(self.header_struct.comp_r03):
                if b == 2 or b == 3:
                    r[c] = 1
                    r[c + 1] = 2
                elif b == 4:
                    r[c] = 1
                    r[c + 1] = 2
                    if self.header_struct.comp_r04 == 0:
                        r[c + 2] = 1
                        r[c + 3] = 2
                elif b == 5:
                    r[c] = 1
                    r[c + 1] = 2
                    if self.header_struct.comp_r04 <= 2:
                        r[c + 3] = 1
                        r[c + 4] = 2
                elif b == 6 or b == 7:
                    r[c] = 1
                    r[c + 1] = 2
                    r[c + 4] = 1
                    r[c + 5] = 2
                elif b == 8:
                    r[c] = 1
                    r[c + 1] = 2
                    r[c + 4] = 1
                    r[c + 5] = 2
                    r[c + 6] = 1
                    r[c + 7] = 2
                c += b

        for i in range(self.header_struct.channel_count):
            self.channels[i].channel_type = r[i]
            self.channels[i].intensity_stereo_offset = (
                self.header_struct.comp_r06 + self.header_struct.comp_r07
            )
            self.channels[i].active_coeffs_count = self.header_struct.comp_r06 + (
                self.header_struct.comp_r07 if r[i] != 2 else 0
            )

    def _read_header(self) -> None:
        """Read and parse the HCA file header structure.

        Parses magic bytes, version, header chunks (fmt, comp/dec, vbr, ath, loop, ciph, rva, comm, pad),
        and verifies the header integrity.
        """
        with open(self.file_path, "rb") as fp:
            # Read magic header
            hca_bytes = bytearray(fp.read(8))

            magic = 0xFFFFFFFF
            sign = struct.unpack("<I", hca_bytes[0:4])[0] & 0x7F7F7F7F
            if sign == 0x00414348:  # "HCA\x00"
                magic = 0x7F7F7F7F
                self.encrypted = True

            sign = struct.unpack("<I", hca_bytes[0:4])[0] & magic
            if sign != 0x00414348:
                raise ValueError("Invalid HCA header")

            # Write unmasked signature back to hca_bytes
            struct.pack_into("<I", hca_bytes, 0, sign)

            self.header_struct.version = struct.unpack(">H", hca_bytes[4:6])[0]
            self.header_struct.data_offset = struct.unpack(">H", hca_bytes[6:8])[0]

            # Read full header
            fp.seek(0)
            self.header_bytes = bytearray(fp.read(self.header_struct.data_offset))

            # Copy unmasked hca_bytes into header
            self.header_bytes[0:8] = hca_bytes[0:8]

            header_offset = 8

            # Parse fmt block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00746D66:  # "fmt\x00"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.channel_count = self.header_bytes[header_offset + 4]

                sampling_rate_bytes = bytearray(4)
                sampling_rate_bytes[:3] = self.header_bytes[header_offset + 5 : header_offset + 8]
                self.header_struct.sampling_rate = struct.unpack(">I", sampling_rate_bytes)[0]
                self.header_struct.block_count = struct.unpack(
                    ">I", self.header_bytes[header_offset + 8 : header_offset + 12]
                )[0]
                header_offset += 16
            else:
                raise ValueError("fmt block not found")

            # Parse comp or dec block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x706D6F63:  # "comp"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.block_size = struct.unpack(
                    ">H", self.header_bytes[header_offset + 4 : header_offset + 6]
                )[0]
                self.header_struct.comp_r01 = self.header_bytes[header_offset + 6]
                self.header_struct.comp_r02 = self.header_bytes[header_offset + 7]
                self.header_struct.comp_r03 = self.header_bytes[header_offset + 8]
                self.header_struct.comp_r04 = self.header_bytes[header_offset + 9]
                self.header_struct.comp_r05 = self.header_bytes[header_offset + 10]
                self.header_struct.comp_r06 = self.header_bytes[header_offset + 11]
                self.header_struct.comp_r07 = self.header_bytes[header_offset + 12]
                self.header_struct.comp_r08 = self.header_bytes[header_offset + 13]
                header_offset += 16

            elif sign == 0x00636564:  # "dec"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.block_size = struct.unpack(
                    ">H", self.header_bytes[header_offset + 4 : header_offset + 6]
                )[0]
                self.header_struct.comp_r01 = self.header_bytes[header_offset + 6]
                self.header_struct.comp_r02 = self.header_bytes[header_offset + 7]
                self.header_struct.comp_r03 = self.header_bytes[header_offset + 10] >> 4
                self.header_struct.comp_r04 = self.header_bytes[header_offset + 10] & 0xF
                self.header_struct.comp_r05 = self.header_bytes[header_offset + 8]
                self.header_struct.comp_r06 = (
                    self.header_bytes[header_offset + 9]
                    if self.header_bytes[header_offset + 11] > 0
                    else self.header_bytes[header_offset + 8]
                ) + 1
                self.header_struct.comp_r07 = (
                    self.header_struct.comp_r05 - self.header_struct.comp_r06
                )
                self.header_struct.comp_r08 = 0
                header_offset += 12
            else:
                raise ValueError("comp/dec block not found")

            if self.header_struct.comp_r03 == 0:
                self.header_struct.comp_r03 = 1

            # Parse optional vbr block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00726276:  # "vbr"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 8

            # Parse optional ath block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00687461:  # "ath"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.ath_type = struct.unpack(
                    ">H", self.header_bytes[header_offset + 4 : header_offset + 6]
                )[0]
                header_offset += 6
            elif self.header_struct.version < 0x200:
                self.header_struct.ath_type = 1

            # Parse optional loop block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x706F6F6C:  # "loop"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.loop_flag = True
                header_offset += 16

            # Parse optional ciph block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x68706963:  # "ciph"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.ciph_type = struct.unpack(
                    ">H", self.header_bytes[header_offset + 4 : header_offset + 6]
                )[0]
                if self.header_struct.ciph_type not in (0, 1, 0x38):
                    raise ValueError(f"Invalid cipher type: {self.header_struct.ciph_type}")
                header_offset += 6

            # Parse optional rva block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00617672:  # "rva"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                self.header_struct.volume = struct.unpack(
                    ">f", self.header_bytes[header_offset + 4 : header_offset + 8]
                )[0]
                header_offset += 8

            # Parse optional comm block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x6D6D6F63:  # "comm"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 5

            # Parse optional pad block
            sign = (
                struct.unpack("<I", self.header_bytes[header_offset : header_offset + 4])[0] & magic
            )
            if sign == 0x00646170:  # "pad"
                struct.pack_into("<I", self.header_bytes, header_offset, sign)
                header_offset += 4

            # Update checksum
            checksum = self._checksum(self.header_bytes[:-2])
            struct.pack_into(">H", self.header_bytes, len(self.header_bytes) - 2, checksum)

            # Read audio data
            self.data = bytearray(
                fp.read(self.header_struct.block_size * self.header_struct.block_count)
            )

        # Initialize tables and channels
        self._ath_init()
        self._init_mask(self.header_struct.ciph_type)
        self._channel_init()

    def decrypt(self) -> None:
        """Decrypt the audio data in-place and save to disk.

        This method applies the initialized cipher mask to each block of the audio data,
        updates the block checksums, and overwrites the input file with the decrypted version.
        If the file is not encrypted (ciph_type == 0), this operation does nothing.
        """
        if self.header_struct.ciph_type == 0:
            return

        for i in range(self.header_struct.block_count):
            offset = i * self.header_struct.block_size
            block = bytearray(self.data[offset : offset + self.header_struct.block_size])
            self._mask(block)

            # Update checksum
            checksum = self._checksum(block[:-2])
            struct.pack_into(">H", block, len(block) - 2, checksum)
            self.data[offset : offset + self.header_struct.block_size] = block

        # Update header to reflect decryption
        self.header_struct.ciph_type = 0
        try:
            ciph_start = self.header_bytes.index(b"ciph")
            struct.pack_into(">H", self.header_bytes, ciph_start + 4, 0)
        except ValueError:
            pass

        # Re-checksum the header
        checksum = self._checksum(self.header_bytes[:-2])
        struct.pack_into(">H", self.header_bytes, len(self.header_bytes) - 2, checksum)

        # Write decrypted HCA back to file
        with open(self.file_path, "wb") as f:
            f.write(self.header_bytes)
            f.write(self.data)

    def convert_to_flac(self, output_path: Path) -> Path:
        """Convert HCA to FLAC using ffmpeg."""
        flac_file = output_path / f"{self.file_path.stem}.flac"

        # Build ffmpeg command - use the already decrypted HCA file
        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output file
            "-loglevel",
            "error",  # Only show errors
            "-i",
            str(self.file_path),
            "-compression_level",
            "8",
            str(flac_file),
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return flac_file
        except subprocess.CalledProcessError as e:
            log.error(f"Error converting audio: {e}")
            if e.stderr:
                log.error(f"{e.stderr}")
            raise typer.Exit(1) from e
        except FileNotFoundError:
            log.error("ffmpeg not found. Place ffmpeg in the root directory and try again.")
            raise typer.Exit(1) from None
