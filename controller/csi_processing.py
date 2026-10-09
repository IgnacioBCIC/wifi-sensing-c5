#!/usr/bin/env python3

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from statistics import mean, median, pstdev
from typing import Sequence


HT20_CSI_LEN = 114
LLTF_CSI_LEN = 106
MIN_BLOCK_COVERAGE = 0.99


LLTF_DOCUMENTED_RAW_SUBCARRIER_INDICES = (
    list(range(0, 27)) + list(range(-26, 0))
)
LLTF_USABLE_SUBCARRIER_INDICES = (
    list(range(-26, 0)) + list(range(1, 27))
)


S3_LLTF_LAYOUT = list(range(0, 32)) + list(range(-32, 0))
S3_ESPECTRE_FEATURE_SUBCARRIERS = tuple(S3_LLTF_LAYOUT[:56])
ESPECTRE_FEATURES = 56


RAW_SUBCARRIER_INDICES = list(range(-28, 29))
DOCUMENTED_RAW_SUBCARRIER_INDICES = (
    list(range(0, 29))
    + list(range(-28, 0))
)


USABLE_SUBCARRIER_INDICES = (
    list(range(-28, 0))
    + list(range(1, 29))
)

PILOT_SUBCARRIERS = {-21, -7, 7, 21}
NUM_USABLE_SUBCARRIERS = len(USABLE_SUBCARRIER_INDICES)


@dataclass(frozen=True)
class DecodedCsiFrame:
    sequence: int
    timestamp_us: int
    rssi: int
    channel: int
    rx_seq: int
    subcarrier_indices: tuple[int, ...]
    real: tuple[float, ...]
    imag: tuple[float, ...]
    amplitude: tuple[float, ...]
    phase: tuple[float, ...]
    first_word_invalid: bool


@dataclass(frozen=True)
class ProcessedCsiBlock:
    block_id: int
    target_frames: int
    sequence_axis: tuple[int, ...]
    subcarrier_indices: tuple[int, ...]

    real_matrix: tuple[tuple[float, ...], ...]
    imag_matrix: tuple[tuple[float, ...], ...]
    amplitude_matrix: tuple[tuple[float, ...], ...]
    phase_matrix: tuple[tuple[float, ...], ...]


    espectre_amplitude_matrix: tuple[tuple[float, ...], ...]


    valid_mask: tuple[bool, ...]


    interpolated_mask: tuple[bool, ...]

    rssi: tuple[float, ...]
    timestamps_us: tuple[float, ...]

    raw_complete: bool
    model_ready: bool
    coverage: float

    missing_sequences: tuple[int, ...]
    interpolated_sequences: tuple[int, ...]
    remaining_missing_sequences: tuple[int, ...]
    nan_values: int

    @property
    def shape(self) -> tuple[int, int]:
        return (
            len(self.amplitude_matrix),
            len(self.subcarrier_indices),
        )


@dataclass(frozen=True)
class ProcessedCsiWindow:
    window_index: int
    block_ids: tuple[int, int]
    subcarrier_indices: tuple[int, ...]

    real_matrix: tuple[tuple[float, ...], ...]
    imag_matrix: tuple[tuple[float, ...], ...]
    amplitude_matrix: tuple[tuple[float, ...], ...]
    phase_matrix: tuple[tuple[float, ...], ...]

    valid_mask: tuple[bool, ...]
    interpolated_mask: tuple[bool, ...]

    model_ready: bool
    raw_coverage: float
    interpolated_samples: int
    nan_values: int

    @property
    def shape(self) -> tuple[int, int]:
        return (
            len(self.amplitude_matrix),
            len(self.subcarrier_indices),
        )


class CsiProcessingError(ValueError):
    pass


def _signed_bytes(raw: bytes) -> tuple[int, ...]:
    return struct.unpack(
        f"{len(raw)}b",
        raw,
    )


def _nan_row(width: int = NUM_USABLE_SUBCARRIERS) -> list[float]:
    return [math.nan] * width


def _row_has_nan(row: Sequence[float]) -> bool:
    return any(math.isnan(value) for value in row)


def _count_nan(matrix: Sequence[Sequence[float]]) -> int:
    return sum(
        math.isnan(value)
        for row in matrix
        for value in row
    )


def decode_ht20_frame(
    *,
    sequence: int,
    timestamp_us: int,
    rssi: int,
    channel: int,
    rx_seq: int,
    csi: bytes,
    first_word_invalid: bool,
) -> DecodedCsiFrame:
    if len(csi) != HT20_CSI_LEN:
        raise CsiProcessingError(
            f"CSI HT-LTF esperado={HT20_CSI_LEN} bytes, recibido={len(csi)}"
        )

    values = _signed_bytes(csi)
    raw_map: dict[int, tuple[float, float]] = {}

    for pair_index, subcarrier in enumerate(RAW_SUBCARRIER_INDICES):
        offset = pair_index * 2
        imag = float(values[offset])
        real = float(values[offset + 1])
        raw_map[subcarrier] = (real, imag)

    if first_word_invalid:
        raw_map[-28] = (math.nan, math.nan)
        raw_map[-27] = (math.nan, math.nan)

    real_values: list[float] = []
    imag_values: list[float] = []
    amplitudes: list[float] = []
    phases: list[float] = []

    for subcarrier in USABLE_SUBCARRIER_INDICES:
        real, imag = raw_map[subcarrier]
        real_values.append(real)
        imag_values.append(imag)
        if math.isnan(real) or math.isnan(imag):
            amplitudes.append(math.nan)
            phases.append(math.nan)
        else:
            amplitudes.append(math.hypot(real, imag))
            phases.append(math.atan2(imag, real))

    return DecodedCsiFrame(
        sequence=sequence,
        timestamp_us=timestamp_us,
        rssi=rssi,
        channel=channel,
        rx_seq=rx_seq,
        subcarrier_indices=tuple(USABLE_SUBCARRIER_INDICES),
        real=tuple(real_values),
        imag=tuple(imag_values),
        amplitude=tuple(amplitudes),
        phase=tuple(phases),
        first_word_invalid=first_word_invalid,
    )


def _lltf_raw_indices(csi: bytes) -> tuple[int, ...]:
    if len(csi) != LLTF_CSI_LEN:
        raise CsiProcessingError(
            f"CSI LLTF esperado={LLTF_CSI_LEN} bytes, recibido={len(csi)}"
        )
    return tuple(LLTF_DOCUMENTED_RAW_SUBCARRIER_INDICES)


def _decode_lltf_raw_map(
    *,
    csi: bytes,
    first_word_invalid: bool,
) -> tuple[dict[int, tuple[float, float]], tuple[int, ...]]:
    raw_indices = _lltf_raw_indices(csi)
    values = _signed_bytes(csi)
    raw_map: dict[int, tuple[float, float]] = {}

    for pair_index, subcarrier in enumerate(raw_indices):
        offset = pair_index * 2
        imag = float(values[offset])
        real = float(values[offset + 1])
        raw_map[subcarrier] = (real, imag)

    if first_word_invalid:
        for pair_index in (0, 1):
            raw_map[raw_indices[pair_index]] = (math.nan, math.nan)

    return raw_map, raw_indices


def decode_lltf_frame(
    *,
    sequence: int,
    timestamp_us: int,
    rssi: int,
    channel: int,
    rx_seq: int,
    csi: bytes,
    first_word_invalid: bool,
) -> DecodedCsiFrame:
    raw_map, _ = _decode_lltf_raw_map(
        csi=csi,
        first_word_invalid=first_word_invalid,
    )

    real_values: list[float] = []
    imag_values: list[float] = []
    amplitudes: list[float] = []
    phases: list[float] = []

    for subcarrier in LLTF_USABLE_SUBCARRIER_INDICES:
        real, imag = raw_map[subcarrier]
        real_values.append(real)
        imag_values.append(imag)
        if math.isnan(real) or math.isnan(imag):
            amplitudes.append(math.nan)
            phases.append(math.nan)
        else:
            amplitudes.append(math.hypot(real, imag))
            phases.append(math.atan2(imag, real))

    return DecodedCsiFrame(
        sequence=sequence,
        timestamp_us=timestamp_us,
        rssi=rssi,
        channel=channel,
        rx_seq=rx_seq,
        subcarrier_indices=tuple(LLTF_USABLE_SUBCARRIER_INDICES),
        real=tuple(real_values),
        imag=tuple(imag_values),
        amplitude=tuple(amplitudes),
        phase=tuple(phases),
        first_word_invalid=first_word_invalid,
    )


def decode_csi_frame(**kwargs) -> DecodedCsiFrame:
    csi = kwargs["csi"]
    if len(csi) == LLTF_CSI_LEN:
        return decode_lltf_frame(**kwargs)
    if len(csi) == HT20_CSI_LEN:
        return decode_ht20_frame(**kwargs)
    raise CsiProcessingError(
        f"longitud CSI no soportada={len(csi)}; "
        f"esperado LLTF={LLTF_CSI_LEN} o HT-LTF={HT20_CSI_LEN}"
    )


def _decode_espectre_compat56_iq(
    *,
    csi: bytes,
    first_word_invalid: bool,
) -> tuple[list[float], list[float]]:
    if len(csi) == LLTF_CSI_LEN:
        raw_map, _ = _decode_lltf_raw_map(
            csi=csi,
            first_word_invalid=first_word_invalid,
        )
        real_values: list[float] = []
        imag_values: list[float] = []

        for subcarrier in S3_ESPECTRE_FEATURE_SUBCARRIERS:


            if subcarrier == 0 or subcarrier < -26 or subcarrier > 26:
                real, imag = 0.0, 0.0
            else:
                real, imag = raw_map[subcarrier]
            real_values.append(real)
            imag_values.append(imag)

        return real_values, imag_values

    if len(csi) == HT20_CSI_LEN:
        values = _signed_bytes(csi)
        real_values: list[float] = []
        imag_values: list[float] = []
        for pair_index in range(ESPECTRE_FEATURES):
            offset = pair_index * 2
            imag = float(values[offset])
            real = float(values[offset + 1])
            if first_word_invalid and pair_index < 2:
                imag = math.nan
                real = math.nan
            real_values.append(real)
            imag_values.append(imag)
        return real_values, imag_values

    raise CsiProcessingError(
        f"longitud CSI no soportada para ESPectre={len(csi)}"
    )


def _missing_runs(missing: Sequence[int]) -> list[tuple[int, int]]:
    """Convierte [3,4,8] en [(3,4), (8,8)]."""

    if not missing:
        return []

    runs: list[tuple[int, int]] = []
    start = previous = missing[0]

    for value in missing[1:]:
        if value == previous + 1:
            previous = value
            continue

        runs.append((start, previous))
        start = previous = value

    runs.append((start, previous))
    return runs


def _interpolate_missing_iq(
    *,
    real_matrix: list[list[float]],
    imag_matrix: list[list[float]],
    missing: Sequence[int],
    target_frames: int,
) -> tuple[list[int], list[int]]:
    interpolated: list[int] = []
    unresolved: list[int] = []

    for start, end in _missing_runs(missing):
        left = start - 1
        right = end + 1

        if left < 0 or right >= target_frames:
            unresolved.extend(range(start, end + 1))
            continue

        left_real = real_matrix[left]
        right_real = real_matrix[right]
        left_imag = imag_matrix[left]
        right_imag = imag_matrix[right]

        if (
            _row_has_nan(left_real)
            or _row_has_nan(right_real)
            or _row_has_nan(left_imag)
            or _row_has_nan(right_imag)
        ):
            unresolved.extend(range(start, end + 1))
            continue

        span = right - left

        for seq in range(start, end + 1):
            alpha = (seq - left) / span

            real_matrix[seq] = [
                a + (b - a) * alpha
                for a, b in zip(left_real, right_real)
            ]
            imag_matrix[seq] = [
                a + (b - a) * alpha
                for a, b in zip(left_imag, right_imag)
            ]

            interpolated.append(seq)

    return interpolated, unresolved


def _derive_amp_phase(
    real_matrix: Sequence[Sequence[float]],
    imag_matrix: Sequence[Sequence[float]],
) -> tuple[list[list[float]], list[list[float]]]:
    amplitude_matrix: list[list[float]] = []
    phase_matrix: list[list[float]] = []

    for real_row, imag_row in zip(real_matrix, imag_matrix):
        amp_row: list[float] = []
        phase_row: list[float] = []

        for real, imag in zip(real_row, imag_row):
            if math.isnan(real) or math.isnan(imag):
                amp_row.append(math.nan)
                phase_row.append(math.nan)
            else:
                amp_row.append(math.hypot(real, imag))
                phase_row.append(math.atan2(imag, real))

        amplitude_matrix.append(amp_row)
        phase_matrix.append(phase_row)

    return amplitude_matrix, phase_matrix


def process_block(
    *,
    block_id: int,
    frames: Sequence,
    target_frames: int,
    min_coverage: float = MIN_BLOCK_COVERAGE,
) -> ProcessedCsiBlock:
    if target_frames <= 0:
        raise CsiProcessingError("target_frames debe ser > 0")

    frame_by_sequence = {
        frame.sequence: frame
        for frame in frames
        if 0 <= frame.sequence < target_frames
    }

    if not frame_by_sequence:
        raise CsiProcessingError("bloque sin frames CSI válidos")

    lengths = {len(frame.csi) for frame in frame_by_sequence.values()}
    if len(lengths) != 1:
        raise CsiProcessingError(
            f"longitudes CSI mezcladas dentro del bloque: {sorted(lengths)}"
        )

    source_len = next(iter(lengths))
    if source_len == LLTF_CSI_LEN:
        analysis_indices = tuple(LLTF_USABLE_SUBCARRIER_INDICES)
    elif source_len == HT20_CSI_LEN:
        analysis_indices = tuple(USABLE_SUBCARRIER_INDICES)
    else:
        raise CsiProcessingError(
            f"longitud CSI no soportada={source_len}; "
            f"LLTF={LLTF_CSI_LEN}, HT-LTF={HT20_CSI_LEN}"
        )

    analysis_width = len(analysis_indices)
    sequence_axis = tuple(range(target_frames))
    valid_mask = [False] * target_frames
    interpolated_mask = [False] * target_frames

    real_matrix = [_nan_row(analysis_width) for _ in range(target_frames)]
    imag_matrix = [_nan_row(analysis_width) for _ in range(target_frames)]


    espectre_real_matrix = [_nan_row(ESPECTRE_FEATURES) for _ in range(target_frames)]
    espectre_imag_matrix = [_nan_row(ESPECTRE_FEATURES) for _ in range(target_frames)]
    rssi = [math.nan] * target_frames
    timestamps_us = [math.nan] * target_frames

    for sequence, frame in frame_by_sequence.items():
        decoded = decode_csi_frame(
            sequence=frame.sequence,
            timestamp_us=frame.timestamp_us,
            rssi=frame.rssi,
            channel=frame.channel,
            rx_seq=frame.rx_seq,
            csi=frame.csi,
            first_word_invalid=frame.first_word_invalid,
        )

        if decoded.subcarrier_indices != analysis_indices:
            raise CsiProcessingError(
                "mapeo de subportadoras cambió dentro del bloque"
            )

        real_matrix[sequence] = list(decoded.real)
        imag_matrix[sequence] = list(decoded.imag)

        espectre_real, espectre_imag = _decode_espectre_compat56_iq(
            csi=frame.csi,
            first_word_invalid=frame.first_word_invalid,
        )
        espectre_real_matrix[sequence] = espectre_real
        espectre_imag_matrix[sequence] = espectre_imag

        rssi[sequence] = float(decoded.rssi)
        timestamps_us[sequence] = float(decoded.timestamp_us)
        valid_mask[sequence] = True

    missing = [
        sequence
        for sequence, valid in enumerate(valid_mask)
        if not valid
    ]

    coverage = sum(valid_mask) / target_frames
    interpolated: list[int] = []
    unresolved = list(missing)

    if missing and coverage >= min_coverage:
        interpolated, unresolved = _interpolate_missing_iq(
            real_matrix=real_matrix,
            imag_matrix=imag_matrix,
            missing=missing,
            target_frames=target_frames,
        )

        espectre_interpolated, espectre_unresolved = _interpolate_missing_iq(
            real_matrix=espectre_real_matrix,
            imag_matrix=espectre_imag_matrix,
            missing=missing,
            target_frames=target_frames,
        )
        if (
            espectre_interpolated != interpolated
            or espectre_unresolved != unresolved
        ):
            raise CsiProcessingError(
                "interpolación temporal inconsistente entre análisis y ESPectre"
            )

        for sequence in interpolated:
            interpolated_mask[sequence] = True
            left = sequence - 1
            while left >= 0 and math.isnan(timestamps_us[left]):
                left -= 1
            right = sequence + 1
            while right < target_frames and math.isnan(timestamps_us[right]):
                right += 1
            if left >= 0 and right < target_frames:
                alpha = (sequence - left) / (right - left)
                timestamps_us[sequence] = (
                    timestamps_us[left]
                    + (timestamps_us[right] - timestamps_us[left]) * alpha
                )
                rssi[sequence] = (
                    rssi[left]
                    + (rssi[right] - rssi[left]) * alpha
                )

    amplitude_matrix, phase_matrix = _derive_amp_phase(
        real_matrix,
        imag_matrix,
    )
    espectre_amplitude_matrix, _ = _derive_amp_phase(
        espectre_real_matrix,
        espectre_imag_matrix,
    )

    nan_values = _count_nan(amplitude_matrix)
    espectre_nan_values = _count_nan(espectre_amplitude_matrix)
    raw_complete = not missing

    model_ready = (
        coverage >= min_coverage
        and not unresolved
        and nan_values == 0
        and espectre_nan_values == 0
    )

    return ProcessedCsiBlock(
        block_id=block_id,
        target_frames=target_frames,
        sequence_axis=sequence_axis,
        subcarrier_indices=analysis_indices,
        real_matrix=tuple(tuple(row) for row in real_matrix),
        imag_matrix=tuple(tuple(row) for row in imag_matrix),
        amplitude_matrix=tuple(tuple(row) for row in amplitude_matrix),
        phase_matrix=tuple(tuple(row) for row in phase_matrix),
        espectre_amplitude_matrix=tuple(
            tuple(row) for row in espectre_amplitude_matrix
        ),
        valid_mask=tuple(valid_mask),
        interpolated_mask=tuple(interpolated_mask),
        rssi=tuple(rssi),
        timestamps_us=tuple(timestamps_us),
        raw_complete=raw_complete,
        model_ready=model_ready,
        coverage=coverage,
        missing_sequences=tuple(missing),
        interpolated_sequences=tuple(interpolated),
        remaining_missing_sequences=tuple(unresolved),
        nan_values=nan_values,
    )


def build_window(
    *,
    first: ProcessedCsiBlock,
    second: ProcessedCsiBlock,
) -> ProcessedCsiWindow:
    if second.block_id != first.block_id + 1:
        raise CsiProcessingError(
            f"bloques no consecutivos: {first.block_id}, {second.block_id}"
        )

    if first.target_frames != second.target_frames:
        raise CsiProcessingError("target_frames distinto entre bloques")

    if first.subcarrier_indices != second.subcarrier_indices:
        raise CsiProcessingError("subportadoras distintas entre bloques")

    expected_first = ((first.block_id - 1) // 2) * 2 + 1
    if first.block_id != expected_first:
        raise CsiProcessingError(
            "la ventana debe comenzar en un bloque impar: 1+2, 3+4, ..."
        )

    valid_mask = first.valid_mask + second.valid_mask
    interpolated_mask = (
        first.interpolated_mask
        + second.interpolated_mask
    )

    amplitude_matrix = (
        first.amplitude_matrix
        + second.amplitude_matrix
    )
    phase_matrix = (
        first.phase_matrix
        + second.phase_matrix
    )
    real_matrix = first.real_matrix + second.real_matrix
    imag_matrix = first.imag_matrix + second.imag_matrix

    total_samples = len(valid_mask)
    raw_coverage = (
        sum(valid_mask) / total_samples
        if total_samples
        else 0.0
    )

    nan_values = _count_nan(amplitude_matrix)

    return ProcessedCsiWindow(
        window_index=(first.block_id + 1) // 2,
        block_ids=(first.block_id, second.block_id),
        subcarrier_indices=first.subcarrier_indices,
        real_matrix=real_matrix,
        imag_matrix=imag_matrix,
        amplitude_matrix=amplitude_matrix,
        phase_matrix=phase_matrix,
        valid_mask=valid_mask,
        interpolated_mask=interpolated_mask,
        model_ready=(
            first.model_ready
            and second.model_ready
            and nan_values == 0
        ),
        raw_coverage=raw_coverage,
        interpolated_samples=sum(interpolated_mask),
        nan_values=nan_values,
    )


def block_amplitude_summary(block: ProcessedCsiBlock) -> dict:
    valid_amplitudes = [
        value
        for row in block.amplitude_matrix
        for value in row
        if not math.isnan(value)
    ]

    if not valid_amplitudes:
        return {
            "mean": None,
            "min": None,
            "max": None,
        }

    return {
        "mean": mean(valid_amplitudes),
        "min": min(valid_amplitudes),
        "max": max(valid_amplitudes),
    }


def window_amplitude_summary(window: ProcessedCsiWindow) -> dict:
    valid_amplitudes = [
        value
        for row in window.amplitude_matrix
        for value in row
        if not math.isnan(value)
    ]

    if not valid_amplitudes:
        return {
            "mean": None,
            "min": None,
            "max": None,
        }

    return {
        "mean": mean(valid_amplitudes),
        "min": min(valid_amplitudes),
        "max": max(valid_amplitudes),
    }


def raw_csi_pair_diagnostics(frames: Sequence) -> dict:
    supported = [
        frame for frame in frames
        if len(frame.csi) in {LLTF_CSI_LEN, HT20_CSI_LEN}
    ]
    if not supported:
        raise CsiProcessingError("no hay frames CSI soportados para auditar")

    lengths = {len(frame.csi) for frame in supported}
    if len(lengths) != 1:
        raise CsiProcessingError(
            f"longitudes CSI mezcladas para auditoría: {sorted(lengths)}"
        )

    csi_len = next(iter(lengths))
    pair_count = csi_len // 2

    if csi_len == LLTF_CSI_LEN:
        ltf_mode = "LLTF"
        documented_indices = tuple(LLTF_DOCUMENTED_RAW_SUBCARRIER_INDICES)
        observed_indices = documented_indices
        focus_indices = (0, 1, 25, 26, 27, 37, 51, 52)
    else:
        ltf_mode = "HT-LTF"
        observed_indices = tuple(RAW_SUBCARRIER_INDICES)
        documented_indices = tuple(DOCUMENTED_RAW_SUBCARRIER_INDICES)
        focus_indices = (0, 1, 27, 28, 29, 30, 55, 56)

    pair_stats = []
    for pair_index in range(pair_count):
        amplitudes = []
        zero_count = 0
        unique_pairs = set()
        offset = pair_index * 2

        for frame in supported:
            values = _signed_bytes(frame.csi[offset:offset + 2])
            imag = int(values[0])
            real = int(values[1])
            unique_pairs.add((imag, real))
            amp = math.hypot(real, imag)
            amplitudes.append(amp)
            if imag == 0 and real == 0:
                zero_count += 1

        pair_stats.append({
            "pair_index": pair_index,
            "documented_subcarrier": documented_indices[pair_index],
            "observed_subcarrier": observed_indices[pair_index],
            "count": len(amplitudes),
            "zero_count": zero_count,
            "zero_fraction": zero_count / len(amplitudes),
            "unique_pairs": len(unique_pairs),
            "mean_amplitude": mean(amplitudes),
            "std_amplitude": pstdev(amplitudes) if len(amplitudes) > 1 else 0.0,
            "min_amplitude": min(amplitudes),
            "max_amplitude": max(amplitudes),
        })

    all_zero = tuple(
        item["pair_index"] for item in pair_stats
        if item["zero_count"] == item["count"]
    )
    all_zero_documented = tuple(
        item["documented_subcarrier"] for item in pair_stats
        if item["zero_count"] == item["count"]
    )
    all_zero_observed = tuple(
        item["observed_subcarrier"] for item in pair_stats
        if item["zero_count"] == item["count"]
    )
    focus = tuple(
        pair_stats[index]
        for index in focus_indices
        if index < len(pair_stats)
    )

    return {
        "ltf_mode": ltf_mode,
        "csi_len": csi_len,
        "frame_count": len(supported),
        "pair_count": pair_count,
        "all_zero_pair_indices": all_zero,
        "all_zero_documented_subcarriers": all_zero_documented,
        "all_zero_observed_subcarriers": all_zero_observed,
        "pair_stats": tuple(pair_stats),
        "focus": focus,
    }


def window_subcarrier_diagnostics(window: ProcessedCsiWindow) -> dict:

    if not window.amplitude_matrix:
        raise CsiProcessingError("ventana sin muestras")

    rows = len(window.amplitude_matrix)
    cols = len(window.subcarrier_indices)

    if cols == 0:
        raise CsiProcessingError("ventana sin subportadoras")

    stats = []
    total_zero = 0
    total_finite = 0

    for col, subcarrier in enumerate(window.subcarrier_indices):
        values = [
            row[col]
            for row in window.amplitude_matrix
            if not math.isnan(row[col])
        ]

        if not values:
            stats.append({
                "subcarrier": subcarrier,
                "count": 0,
                "mean": math.nan,
                "std": math.nan,
                "min": math.nan,
                "max": math.nan,
                "zero_count": 0,
                "zero_fraction": math.nan,
            })
            continue

        zero_count = sum(value == 0.0 for value in values)
        total_zero += zero_count
        total_finite += len(values)

        stats.append({
            "subcarrier": subcarrier,
            "count": len(values),
            "mean": mean(values),
            "std": pstdev(values) if len(values) > 1 else 0.0,
            "min": min(values),
            "max": max(values),
            "zero_count": zero_count,
            "zero_fraction": zero_count / len(values),
        })

    finite_stats = [
        item for item in stats
        if not math.isnan(item["std"])
    ]

    by_std = sorted(
        finite_stats,
        key=lambda item: item["std"],
    )

    std_values = [item["std"] for item in finite_stats]

    pilot_stats = {
        item["subcarrier"]: item
        for item in stats
        if item["subcarrier"] in PILOT_SUBCARRIERS
    }


    constant_subcarriers = [
        item["subcarrier"]
        for item in finite_stats
        if item["std"] == 0.0
    ]

    return {
        "shape": (rows, cols),
        "subcarriers": tuple(stats),
        "total_zero_values": total_zero,
        "total_finite_values": total_finite,
        "zero_fraction": (
            total_zero / total_finite
            if total_finite
            else math.nan
        ),
        "std_min": min(std_values) if std_values else math.nan,
        "std_median": median(std_values) if std_values else math.nan,
        "std_max": max(std_values) if std_values else math.nan,
        "least_variable": tuple(by_std[:5]),
        "most_variable": tuple(reversed(by_std[-5:])),
        "pilot_stats": pilot_stats,
        "constant_subcarriers": tuple(constant_subcarriers),
    }


@dataclass(frozen=True)
class CsiBaselineProfile:
    subcarrier_indices: tuple[int, ...]
    reference_amplitude: tuple[float, ...]
    temporal_std: tuple[float, ...]
    source_windows: int
    source_samples: int


@dataclass(frozen=True)
class NormalizedCsiWindow:
    window_index: int
    block_ids: tuple[int, int]
    subcarrier_indices: tuple[int, ...]
    delta_db_matrix: tuple[tuple[float, ...], ...]
    model_ready: bool
    nan_values: int

    @property
    def shape(self) -> tuple[int, int]:
        return (
            len(self.delta_db_matrix),
            len(self.subcarrier_indices),
        )


def build_baseline_profile(
    windows: Sequence[ProcessedCsiWindow],
) -> CsiBaselineProfile:
    if not windows:
        raise CsiProcessingError("se requiere al menos una ventana baseline")

    first = windows[0]
    subcarriers = first.subcarrier_indices

    for window in windows:
        if not window.model_ready:
            raise CsiProcessingError(
                f"ventana baseline {window.window_index} no es model-ready"
            )
        if window.subcarrier_indices != subcarriers:
            raise CsiProcessingError("subportadoras incompatibles entre ventanas baseline")

    columns: list[list[float]] = [
        [] for _ in subcarriers
    ]

    for window in windows:
        for row in window.amplitude_matrix:
            for col, value in enumerate(row):
                if not math.isnan(value):
                    columns[col].append(value)

    references: list[float] = []
    temporal_std: list[float] = []

    for subcarrier, values in zip(subcarriers, columns):
        if not values:
            raise CsiProcessingError(
                f"baseline sin datos para subportadora {subcarrier:+d}"
            )

        reference = median(values)
        if reference <= 0.0:
            raise CsiProcessingError(
                f"baseline no positivo para subportadora {subcarrier:+d}: {reference}"
            )

        references.append(float(reference))
        temporal_std.append(
            float(pstdev(values)) if len(values) > 1 else 0.0
        )

    return CsiBaselineProfile(
        subcarrier_indices=tuple(subcarriers),
        reference_amplitude=tuple(references),
        temporal_std=tuple(temporal_std),
        source_windows=len(windows),
        source_samples=sum(len(w.amplitude_matrix) for w in windows),
    )


def normalize_window_to_baseline(
    window: ProcessedCsiWindow,
    baseline: CsiBaselineProfile,
    *,
    epsilon: float = 1e-6,
) -> NormalizedCsiWindow:
    if window.subcarrier_indices != baseline.subcarrier_indices:
        raise CsiProcessingError("baseline y ventana tienen subportadoras distintas")

    rows: list[tuple[float, ...]] = []

    for row in window.amplitude_matrix:
        normalized_row: list[float] = []

        for value, reference in zip(row, baseline.reference_amplitude):
            if math.isnan(value):
                normalized_row.append(math.nan)
                continue

            numerator = max(float(value), epsilon)
            denominator = max(float(reference), epsilon)
            normalized_row.append(
                20.0 * math.log10(numerator / denominator)
            )

        rows.append(tuple(normalized_row))

    nan_values = _count_nan(rows)

    return NormalizedCsiWindow(
        window_index=window.window_index,
        block_ids=window.block_ids,
        subcarrier_indices=window.subcarrier_indices,
        delta_db_matrix=tuple(rows),
        model_ready=(window.model_ready and nan_values == 0),
        nan_values=nan_values,
    )


def normalized_window_summary(window: NormalizedCsiWindow) -> dict:
    values = [
        value
        for row in window.delta_db_matrix
        for value in row
        if not math.isnan(value)
    ]

    if not values:
        return {
            "mean_db": None,
            "std_db": None,
            "min_db": None,
            "max_db": None,
        }

    return {
        "mean_db": mean(values),
        "std_db": pstdev(values) if len(values) > 1 else 0.0,
        "min_db": min(values),
        "max_db": max(values),
    }
