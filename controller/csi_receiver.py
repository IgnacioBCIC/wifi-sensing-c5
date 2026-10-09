#!/usr/bin/env python3


import argparse
import json
import queue
import socket
import struct
import sys
import threading
from pathlib import Path
from collections import Counter
from dataclasses import dataclass, field
from statistics import mean


try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


import numpy as np
import paho.mqtt.client as mqtt

from settings import (
    MQTT_HOST,
    MQTT_PORT,
    MQTT_SERVER_USER,
    MQTT_SERVER_PASSWORD,
    MQTT_BASE_TOPIC,
    SERVER_NAME,
    CSI_UDP_PORT,
)


from pretrained_espectre import (
    EspectreModelError,
    EspectreZeroShot,
)


from ae_oneclass import OneClassAERuntime, build_ae_block


from csi_processing import (
    CsiProcessingError,
    HT20_CSI_LEN,
    LLTF_CSI_LEN,
    block_amplitude_summary,
    build_window,
    process_block,
    raw_csi_pair_diagnostics,
    window_amplitude_summary,
    window_subcarrier_diagnostics,
    build_baseline_profile,
    normalize_window_to_baseline,
    normalized_window_summary,
)


MAGIC = b"WCSI"
VERSION = 1

CSI2_MAGIC = b"CSI2"
CSI2_VERSION = 2
CSI2_HEADER = struct.Struct("!4sB3x4sQIIIIbBBHH")
CSI2_FINALIZE_DELAY_S = 0.25


CSI_FROZEN_MIN_FRAMES = 20
CSI_FROZEN_MAX_UNIQUE_VECTORS = 2


CSI_SUSPICIOUS_UNIQUE_RATIO = 0.10

CHUNK_HEADER = struct.Struct(
    "!4sBBHQIHHHHHH"
)

FRAME_HEADER = struct.Struct(
    "!IIbBBBHH"
)


@dataclass
class CsiFrame:
    sequence: int
    timestamp_us: int
    rssi: int
    channel: int
    first_word_invalid: bool
    rx_seq: int
    csi: bytes


@dataclass(frozen=True)
class CsiSignalHealth:
    status: str
    signal_valid: bool
    warning: bool
    frame_count: int
    unique_vectors: int
    unique_ratio: float
    reason: str | None = None


def evaluate_signal_health(
    *,
    frame_count: int,
    unique_vectors: int,
    min_frames: int = CSI_FROZEN_MIN_FRAMES,
    frozen_max_unique_vectors: int = CSI_FROZEN_MAX_UNIQUE_VECTORS,
    suspicious_unique_ratio: float = CSI_SUSPICIOUS_UNIQUE_RATIO,
) -> CsiSignalHealth:
    """Evalua si un bloque contiene CSI util para ventanas/modelos."""

    ratio = (
        unique_vectors / frame_count
        if frame_count > 0
        else 0.0
    )

    if frame_count < min_frames:
        return CsiSignalHealth(
            status="INSUFFICIENT",
            signal_valid=False,
            warning=True,
            frame_count=frame_count,
            unique_vectors=unique_vectors,
            unique_ratio=ratio,
            reason=(
                f"solo {frame_count} frames; se requieren al menos "
                f"{min_frames} para validar sanidad CSI"
            ),
        )

    if unique_vectors <= frozen_max_unique_vectors:
        return CsiSignalHealth(
            status="CSI_FROZEN",
            signal_valid=False,
            warning=False,
            frame_count=frame_count,
            unique_vectors=unique_vectors,
            unique_ratio=ratio,
            reason=(
                "buffer CSI practicamente constante: "
                f"{unique_vectors} vector(es) unico(s) en "
                f"{frame_count} frames"
            ),
        )

    if ratio < suspicious_unique_ratio:
        return CsiSignalHealth(
            status="SUSPICIOUS",
            signal_valid=True,
            warning=True,
            frame_count=frame_count,
            unique_vectors=unique_vectors,
            unique_ratio=ratio,
            reason=(
                "variacion CSI baja; revisar antes de usar como "
                "dato experimental definitivo"
            ),
        )

    return CsiSignalHealth(
        status="PASS",
        signal_valid=True,
        warning=False,
        frame_count=frame_count,
        unique_vectors=unique_vectors,
        unique_ratio=ratio,
        reason=None,
    )


@dataclass
class Capture:
    device_id: str
    config_id: int
    block_id: int
    chunk_count: int
    total_frames: int
    target_frames: int
    chunks: set[int] = field(default_factory=set)
    frames: dict[int, CsiFrame] = field(default_factory=dict)
    status: dict | None = None
    reported: bool = False

    @property
    def chunks_complete(self) -> bool:
        return (
            self.chunk_count > 0
            and len(self.chunks) == self.chunk_count
        )


class CsiReceiver:

    def __init__(
        self,
        once: bool,
        baseline_windows: int = 0,
        espectre_model_dir: str | None = None,
        espectre_devices: tuple[str, ...] = ("rx01", "rx02", "rx03"),
        espectre_baseline: str | None = None,
        calibration_output: str | None = None,
        calibration_name: str | None = None,
        calibration_target_blocks: int = 13,
        ae_capture_dir: str | None = None,
        ae_condition: str | None = None,
        ae_session_id: str | None = None,
        ae_model_dir: str | None = None,
        ae_devices: tuple[str, ...] = ("rx01", "rx02", "rx03"),
    ):
        self.once = once
        self.baseline_windows_required = baseline_windows
        self.done = threading.Event()


        self.decision_queue = queue.Queue()
        self.decision_worker = None
        self.udp_stop = threading.Event()
        self.udp_thread = None
        self.capture_lock = threading.RLock()
        self.udp_transport = {}
        self.udp_bad_packets = 0

        self.configs = {}
        self.captures = {}
        self.pending_status = {}


        self.pending_experiment_blocks = {}


        self.processed_blocks = {}
        self.processed_windows = {}


        self.baseline_buffers = {}
        self.baselines = {}


        self.espectre = None
        self.espectre_devices = tuple(espectre_devices)
        self.espectre_baseline = espectre_baseline

        self.calibration_output = (
            Path(calibration_output)
            if calibration_output
            else None
        )
        self.calibration_name = calibration_name or "baseline"
        self.calibration_target_blocks = int(calibration_target_blocks)
        self.calibration_rows = []
        self.calibration_accepted_blocks = 0
        self.espectre_pending_blocks = {}
        self.espectre_reported = set()
        self.espectre_invalid_blocks = {}


        self.ae_capture_dir = Path(ae_capture_dir) if ae_capture_dir else None
        self.ae_condition = ae_condition
        self.ae_session_id = ae_session_id or "unknown"
        self.ae_devices = tuple(ae_devices)
        self.ae_runtime = OneClassAERuntime(ae_model_dir) if ae_model_dir else None
        if self.ae_capture_dir is not None:
            (self.ae_capture_dir / "samples").mkdir(parents=True, exist_ok=True)
            print(f"[AE] Captura activa: {self.ae_capture_dir} condition={self.ae_condition}")
        if self.ae_runtime is not None:
            print(f"[AE] Modelo one-class activo: {ae_model_dir}")
            print(f"[AE] threshold={self.ae_runtime.threshold:.6f}")


        self.signal_health = {}

        if espectre_model_dir:
            try:
                self.espectre = EspectreZeroShot(
                    model_dir=espectre_model_dir,
                    device_order=self.espectre_devices,
                    baseline_path=self.espectre_baseline,
                )
                print(
                    "[ESPECTRE] Modelo preentrenado cargado "
                    f"desde {espectre_model_dir}"
                )
                print(
                    "[ESPECTRE] Receptores esperados: "
                    + ", ".join(self.espectre_devices)
                )
                print(
                    "[ESPECTRE] Pesos congelados; modo zero-shot."
                )
                print(
                    f"[ESPECTRE] Baseline activo: {self.espectre.baseline_path}"
                )
            except EspectreModelError as exc:
                raise SystemExit(
                    f"[ESPECTRE] No se pudo cargar el modelo: {exc}"
                )

        client_id = f"{SERVER_NAME}-csi-receiver"

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )

        self.client.username_pw_set(
            MQTT_SERVER_USER,
            MQTT_SERVER_PASSWORD,
        )

        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def on_connect(
        self,
        client,
        userdata,
        flags,
        reason_code,
        properties,
    ):
        if reason_code != 0:
            print(
                f"[ERROR] MQTT rechazó conexión: {reason_code}"
            )
            return

        print(
            f"[MQTT] Conectado a {MQTT_HOST}:{MQTT_PORT}"
        )

        topics = [
            (f"{MQTT_BASE_TOPIC}/control/config", 1),
            (f"{MQTT_BASE_TOPIC}/control/block-decision", 1),
            (f"{MQTT_BASE_TOPIC}/devices/+/status", 1),
        ]

        client.subscribe(topics)

        print("[MQTT] Control conectado; CSI crudo llega por UDP CSI2...")

    def on_disconnect(
        self,
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties,
    ):
        if reason_code != 0:
            print(
                f"[MQTT] Desconectado inesperadamente: {reason_code}"
            )

    def on_message(
        self,
        client,
        userdata,
        msg,
    ):
        config_topic = (
            f"{MQTT_BASE_TOPIC}/control/config"
        )
        decision_topic = (
            f"{MQTT_BASE_TOPIC}/control/block-decision"
        )

        if msg.topic == config_topic:
            self.process_config(msg.payload)
            return

        if msg.topic == decision_topic:
            self.process_block_decision(msg.payload)
            return

        status_suffix = "/status"
        prefix = f"{MQTT_BASE_TOPIC}/devices/"

        if not msg.topic.startswith(prefix):
            return

        if msg.topic.endswith(status_suffix):
            device_id = msg.topic[
                len(prefix):
                -len(status_suffix)
            ]
            self.process_status(
                device_id,
                msg.payload,
            )

    def process_config(self, raw: bytes):
        try:
            payload = json.loads(
                raw.decode("utf-8")
            )
        except Exception as exc:
            print(
                f"[WARN] CONFIG inválido: {exc}"
            )
            return

        config_id = payload.get("config_id")

        if not isinstance(config_id, int):
            return

        self.configs[config_id] = payload

        sensing = payload.get("sensing", {})

        print(
            f"[CONFIG] id={config_id} "
            f"band={sensing.get('band')} "
            f"channel={sensing.get('expected_channel')} "
            f"BW={sensing.get('bandwidth_mhz')} "
            f"purpose={payload.get('purpose', 'experiment')}"
        )

    def get_health_thresholds(self, config: dict):
        health = config.get("healthcheck", {})
        return {
            "min_frames": int(
                health.get(
                    "min_frames",
                    CSI_FROZEN_MIN_FRAMES,
                )
            ),
            "frozen_max_unique_vectors": int(
                health.get(
                    "frozen_max_unique_vectors",
                    CSI_FROZEN_MAX_UNIQUE_VECTORS,
                )
            ),
            "suspicious_unique_ratio": float(
                health.get(
                    "suspicious_unique_ratio",
                    CSI_SUSPICIOUS_UNIQUE_RATIO,
                )
            ),
        }

    def publish_signal_health(
        self,
        *,
        device_id: str,
        config_id: int,
        session_id: str,
        block_id: int,
        health: CsiSignalHealth,
        purpose: str,
        processed=None,
        processing_error: str | None = None,
    ):
        if purpose == "healthcheck":
            quality_status = "HEALTHCHECK"
            model_ready = None
            accepted = health.signal_valid
            coverage = (
                health.frame_count / max(health.frame_count, 1)
                if health.frame_count
                else 0.0
            )
            raw_complete = None
            missing_sequences = []
            interpolated_sequences = []
            remaining_missing_sequences = []
            nan_values = None
        elif processed is None:
            quality_status = "INVALID"
            model_ready = False
            accepted = False
            coverage = 0.0
            raw_complete = False
            missing_sequences = []
            interpolated_sequences = []
            remaining_missing_sequences = []
            nan_values = None
        else:
            model_ready = bool(processed.model_ready)
            accepted = bool(health.signal_valid and model_ready)
            coverage = float(processed.coverage)
            raw_complete = bool(processed.raw_complete)
            missing_sequences = list(processed.missing_sequences)
            interpolated_sequences = list(processed.interpolated_sequences)
            remaining_missing_sequences = list(
                processed.remaining_missing_sequences
            )
            nan_values = int(processed.nan_values)

            if accepted and raw_complete:
                quality_status = "ACCEPT_RAW"
            elif accepted:
                quality_status = "ACCEPT_INTERPOLATED"
            else:
                quality_status = "INVALID"

        payload = {
            "device_id": device_id,
            "config_id": config_id,
            "session_id": session_id,
            "block_id": block_id,
            "purpose": purpose,
            "status": health.status,
            "signal_valid": health.signal_valid,
            "warning": health.warning,
            "frame_count": health.frame_count,
            "unique_vectors": health.unique_vectors,
            "unique_ratio": health.unique_ratio,
            "reason": health.reason,
            "quality_status": quality_status,
            "accepted": accepted,
            "model_ready": model_ready,
            "coverage": coverage,
            "raw_complete": raw_complete,
            "missing_sequences": missing_sequences,
            "interpolated_sequences": interpolated_sequences,
            "remaining_missing_sequences": remaining_missing_sequences,
            "nan_values": nan_values,
            "processing_error": processing_error,
        }

        info = self.client.publish(
            f"{MQTT_BASE_TOPIC}/control/csi-health",
            json.dumps(
                payload,
                separators=(",", ":"),
            ),
            qos=1,
            retain=False,
        )

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            print(
                "[WARN] No se pudo publicar resultado "
                f"de sanidad/calidad CSI: rc={info.rc}"
            )

    def parse_chunk(self, raw: bytes):
        if len(raw) < CHUNK_HEADER.size:
            raise ValueError(
                "chunk menor que cabecera WCSI"
            )

        (
            magic,
            version,
            flags,
            header_len,
            config_id,
            block_id,
            chunk_index,
            chunk_count,
            frame_count,
            total_frames,
            target_frames,
            reserved,
        ) = CHUNK_HEADER.unpack_from(raw, 0)

        if magic != MAGIC:
            raise ValueError(
                f"magic inválido: {magic!r}"
            )

        if version != VERSION:
            raise ValueError(
                f"versión WCSI no soportada: {version}"
            )

        if header_len != CHUNK_HEADER.size:
            raise ValueError(
                f"header_len inválido: {header_len}"
            )

        offset = header_len
        frames = []

        for _ in range(frame_count):
            if offset + FRAME_HEADER.size > len(raw):
                raise ValueError(
                    "frame header truncado"
                )

            (
                sequence,
                timestamp_us,
                rssi,
                channel,
                first_word_invalid,
                frame_reserved,
                rx_seq,
                csi_len,
            ) = FRAME_HEADER.unpack_from(
                raw,
                offset,
            )

            offset += FRAME_HEADER.size

            if offset + csi_len > len(raw):
                raise ValueError(
                    "vector CSI truncado"
                )

            csi = bytes(
                raw[offset:offset + csi_len]
            )

            offset += csi_len

            frames.append(
                CsiFrame(
                    sequence=sequence,
                    timestamp_us=timestamp_us,
                    rssi=rssi,
                    channel=channel,
                    first_word_invalid=bool(
                        first_word_invalid
                    ),
                    rx_seq=rx_seq,
                    csi=csi,
                )
            )

        if offset != len(raw):
            raise ValueError(
                f"bytes adicionales al final: {len(raw) - offset}"
            )

        return {
            "config_id": config_id,
            "block_id": block_id,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "frame_count": frame_count,
            "total_frames": total_frames,
            "target_frames": target_frames,
            "frames": frames,
        }

    def process_chunk(
        self,
        device_id: str,
        raw: bytes,
    ):
        try:
            data = self.parse_chunk(raw)
        except Exception as exc:
            print(
                f"[ERROR] Chunk CSI inválido de {device_id}: {exc}"
            )
            return

        key = (
            device_id,
            data["config_id"],
            data["block_id"],
        )

        capture = self.captures.get(key)

        if capture is None:
            capture = Capture(
                device_id=device_id,
                config_id=data["config_id"],
                block_id=data["block_id"],
                chunk_count=data["chunk_count"],
                total_frames=data["total_frames"],
                target_frames=data["target_frames"],
            )

            capture.status = (
                self.pending_status.pop(
                    key,
                    None,
                )
            )

            self.captures[key] = capture

        if (
            data["chunk_count"]
            != capture.chunk_count
        ):
            print(
                f"[ERROR] {device_id}: chunk_count cambió dentro del bloque"
            )
            return

        chunk_index = data["chunk_index"]

        if chunk_index in capture.chunks:
            print(
                f"[MQTT] Chunk duplicado ignorado: "
                f"{chunk_index + 1}/{capture.chunk_count}"
            )
            return

        capture.chunks.add(chunk_index)

        for frame in data["frames"]:
            capture.frames.setdefault(
                frame.sequence,
                frame,
            )

        print(
            f"[CSI] {device_id} "
            f"config={capture.config_id} "
            f"block={capture.block_id} "
            f"chunk={chunk_index + 1}/{capture.chunk_count} "
            f"frames={len(data['frames'])}"
        )

        self.try_report(key)

    def _update_transport_stats(self, device_id: str, sequence: int):
        stats = self.udp_transport.setdefault(
            device_id,
            {
                "last": None,
                "received": 0,
                "lost": 0,
                "duplicates": 0,
                "out_of_order": 0,
            },
        )

        stats["received"] += 1
        last = stats["last"]

        if last is None:
            stats["last"] = sequence
        elif sequence == last:
            stats["duplicates"] += 1
        elif sequence > last:
            if sequence > last + 1:
                stats["lost"] += sequence - last - 1
            stats["last"] = sequence
        else:

            if sequence <= 3 and last > 10:
                stats["last"] = sequence
            else:
                stats["out_of_order"] += 1

    def _schedule_udp_report(self, key):
        def finalize():
            self.try_report(key)

        timer = threading.Timer(
            CSI2_FINALIZE_DELAY_S,
            finalize,
        )
        timer.daemon = True
        timer.start()

    def process_udp_csi(self, raw: bytes, addr):
        if len(raw) < CSI2_HEADER.size:
            self.udp_bad_packets += 1
            return

        try:
            (
                magic,
                version,
                device_raw,
                config_id,
                block_id,
                probe_sequence,
                transport_sequence,
                timestamp_us,
                rssi,
                flags,
                channel,
                rx_seq,
                csi_len,
            ) = CSI2_HEADER.unpack_from(raw, 0)
        except struct.error:
            self.udp_bad_packets += 1
            return

        if magic != CSI2_MAGIC or version != CSI2_VERSION:
            self.udp_bad_packets += 1
            return

        if len(raw) != CSI2_HEADER.size + csi_len:
            self.udp_bad_packets += 1
            return

        try:
            device_id = device_raw.rstrip(b"\\0").decode("ascii")
        except UnicodeDecodeError:
            self.udp_bad_packets += 1
            return

        if device_id not in {"rx01", "rx02", "rx03"}:
            self.udp_bad_packets += 1
            return

        csi = bytes(raw[CSI2_HEADER.size:])

        self._update_transport_stats(
            device_id,
            transport_sequence,
        )

        frame = CsiFrame(
            sequence=probe_sequence,
            timestamp_us=timestamp_us,
            rssi=rssi,
            channel=channel,
            first_word_invalid=bool(flags & 0x01),
            rx_seq=rx_seq,
            csi=csi,
        )

        key = (
            device_id,
            int(config_id),
            int(block_id),
        )

        with self.capture_lock:
            capture = self.captures.get(key)

            if capture is None:
                config = self.configs.get(int(config_id), {})
                target = int(
                    config.get("capture", {}).get(
                        "target_packets",
                        0,
                    )
                )

                capture = Capture(
                    device_id=device_id,
                    config_id=int(config_id),
                    block_id=int(block_id),
                    chunk_count=1,
                    total_frames=0,
                    target_frames=target,
                )
                capture.chunks.add(0)
                capture.status = self.pending_status.pop(
                    key,
                    None,
                )
                self.captures[key] = capture

            if capture.reported:
                return

            capture.frames.setdefault(
                probe_sequence,
                frame,
            )
            capture.total_frames = len(capture.frames)

            if capture.status is not None:
                self._schedule_udp_report(key)

    def udp_loop(self):
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )
        sock.bind(("0.0.0.0", CSI_UDP_PORT))
        sock.settimeout(0.5)

        print(
            f"[CSI2] UDP receiver escuchando en 0.0.0.0:{CSI_UDP_PORT}",
            flush=True,
        )

        try:
            while not self.udp_stop.is_set():
                try:
                    raw, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break

                self.process_udp_csi(raw, addr)
        finally:
            sock.close()

    def process_status(
        self,
        device_id: str,
        raw: bytes,
    ):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return

        if payload.get("state") != "CSI_COMPLETE":
            return

        config_id = payload.get("config_id")
        block_id = payload.get("block_id")

        if not isinstance(config_id, int) or not isinstance(block_id, int):
            return

        key = (device_id, config_id, block_id)

        with self.capture_lock:
            capture = self.captures.get(key)

            if capture is None:
                target_frames = int(payload.get("target_frames") or 0)
                capture = Capture(
                    device_id=device_id,
                    config_id=config_id,
                    block_id=block_id,
                    chunk_count=1,
                    total_frames=0,
                    target_frames=target_frames,
                )
                capture.chunks.add(0)
                self.captures[key] = capture

            capture.status = payload
            capture.total_frames = int(payload.get("frames") or len(capture.frames))
            if capture.target_frames <= 0:
                capture.target_frames = int(payload.get("target_frames") or 0)

        self._schedule_udp_report(key)

    def try_report(self, key):
        with self.capture_lock:
            capture = self.captures.get(key)
            if capture is None or capture.reported or capture.status is None:
                return
            capture.reported = True

        self.report_capture(capture)

        if self.once:
            self.done.set()

    def report_empty(
        self,
        device_id: str,
        status: dict,
    ):
        config_id = status.get("config_id")
        block_id = status.get("block_id")
        session_id = status.get("session_id") or "unknown"

        config = (
            self.configs.get(config_id, {})
            if isinstance(config_id, int)
            else {}
        )
        thresholds = self.get_health_thresholds(config)

        health = evaluate_signal_health(
            frame_count=0,
            unique_vectors=0,
            **thresholds,
        )

        purpose = str(
            config.get(
                "purpose",
                (
                    "healthcheck"
                    if str(session_id).startswith("health_")
                    else "experiment"
                ),
            )
        )

        if isinstance(config_id, int) and isinstance(block_id, int):
            self.signal_health[(device_id, config_id, block_id)] = health
            self.publish_signal_health(
                device_id=device_id,
                config_id=config_id,
                session_id=session_id,
                block_id=block_id,
                health=health,
                purpose=purpose,
                processed=None,
                processing_error="NO_CSI_FRAMES",
            )

        print()
        print("======================================")
        print(" CSI BLOCK SIN FRAMES")
        print("======================================")
        print(f"Dispositivo       : {device_id}")
        print(f"Config ID         : {config_id}")
        print(f"Session ID        : {session_id}")
        print(f"Block ID          : {block_id}")
        print(f"Purpose           : {purpose}")
        print(f"Estado CSI        : {health.status}")
        print("Calidad bloque    : INVALID")
        print(f"Callbacks CSI     : {status.get('callbacks_total')}")
        print(f"Desde AP          : {status.get('source_matches')}")
        print(f"Probe WSEN        : {status.get('probe_matches')}")
        print("======================================")

        if self.once:
            self.done.set()

    def report_capture(
        self,
        capture: Capture,
    ):
        frames = [
            capture.frames[k]
            for k in sorted(capture.frames)
        ]
        sequences = [frame.sequence for frame in frames]
        expected = set(range(capture.target_frames))
        missing = sorted(expected - set(sequences))
        lengths = Counter(len(frame.csi) for frame in frames)
        rssis = [frame.rssi for frame in frames]
        channels = Counter(frame.channel for frame in frames)
        first_invalid = sum(frame.first_word_invalid for frame in frames)
        unique_vectors = len({frame.csi for frame in frames})

        config = self.configs.get(capture.config_id, {})
        sensing = config.get("sensing", {})
        status = capture.status or {}
        session_id = (
            status.get("session_id")
            or config.get("session_id")
            or "unknown"
        )
        purpose = str(
            config.get(
                "purpose",
                (
                    "healthcheck"
                    if str(session_id).startswith("health_")
                    else "experiment"
                ),
            )
        )

        thresholds = self.get_health_thresholds(config)
        signal_health = evaluate_signal_health(
            frame_count=len(frames),
            unique_vectors=unique_vectors,
            **thresholds,
        )
        self.signal_health[(
            capture.device_id,
            capture.config_id,
            capture.block_id,
        )] = signal_health

        intervals_ms = []
        for previous, current in zip(frames, frames[1:]):
            delta_us = (
                current.timestamp_us - previous.timestamp_us
            ) & 0xFFFFFFFF
            intervals_ms.append(delta_us / 1000.0)


        if purpose == "healthcheck":
            self.publish_signal_health(
                device_id=capture.device_id,
                config_id=capture.config_id,
                session_id=session_id,
                block_id=capture.block_id,
                health=signal_health,
                purpose=purpose,
            )

            print()
            print("======================================")
            print(" CSI HEALTHCHECK RECIBIDO")
            print("======================================")
            print(f"Dispositivo       : {capture.device_id}")
            print(f"Config ID         : {capture.config_id}")
            print(f"Session ID        : {session_id}")
            print(f"Block ID          : {capture.block_id}")
            print(f"Banda             : {sensing.get('band')}")
            print(f"Canal config      : {sensing.get('expected_channel')}")
            print(f"Canales observados: {dict(channels)}")
            print(f"Frames recibidos  : {len(frames)}/{capture.target_frames}")
            print(f"Vectores únicos   : {unique_vectors}/{len(frames)}")
            print(f"Estado            : {signal_health.status}")
            print(f"Ratio únicos      : {signal_health.unique_ratio * 100:.2f} %")
            if signal_health.reason:
                print(f"Detalle           : {signal_health.reason}")
            print("Uso               : diagnóstico; NO ventanas/baseline/inferencia")
            print("======================================")
            return

        processed = None
        processing_error = None
        try:
            processed = process_block(
                block_id=capture.block_id,
                frames=frames,
                target_frames=capture.target_frames,
            )
        except CsiProcessingError as exc:
            processing_error = str(exc)


        preaccepted = bool(
            processed is not None
            and signal_health.signal_valid
            and processed.model_ready
        )

        if preaccepted:
            key = (
                capture.config_id,
                session_id,
                capture.block_id,
            )
            bucket = self.pending_experiment_blocks.setdefault(
                key,
                {},
            )
            bucket[capture.device_id] = {
                "capture": capture,
                "status": status,
                "processed": processed,
                "sensing": sensing,
            }

        self.publish_signal_health(
            device_id=capture.device_id,
            config_id=capture.config_id,
            session_id=session_id,
            block_id=capture.block_id,
            health=signal_health,
            purpose=purpose,
            processed=processed,
            processing_error=processing_error,
        )

        if self.calibration_output is not None or self._is_continuous_block(
            config_id=capture.config_id,
            block_id=capture.block_id,
        ):
            quality_status = "INVALID"
            if (
                processed is not None
                and signal_health.signal_valid
                and processed.model_ready
            ):
                quality_status = (
                    "ACCEPT_RAW"
                    if processed.raw_complete
                    else "ACCEPT_INTERPOLATED"
                )

            length_set = sorted(lengths)
            if length_set == [LLTF_CSI_LEN]:
                ltf_mode = "LLTF"
            elif length_set == [HT20_CSI_LEN]:
                ltf_mode = "HT-LTF"
            else:
                ltf_mode = f"MIXED{length_set}"

            print(
                f"[LIVE-QUALITY] {capture.device_id} "
                f"block={capture.block_id} "
                f"signal={signal_health.status} "
                f"quality={quality_status} "
                f"frames={len(frames)}/{capture.target_frames} "
                f"ltf={ltf_mode} csi_len={length_set}"
            )
            return

        print()
        print("======================================")
        print(" CSI BLOCK RECIBIDO")
        print("======================================")
        print(f"Dispositivo       : {capture.device_id}")
        print(f"Config ID         : {capture.config_id}")
        print(f"Session ID        : {session_id}")
        print(f"Block ID          : {capture.block_id}")
        print(f"Banda             : {sensing.get('band')}")
        print(f"Canal config      : {sensing.get('expected_channel')}")
        print(f"Canales observados: {dict(channels)}")
        print(f"Frames objetivo   : {capture.target_frames}")
        print(f"Frames recibidos  : {len(frames)}")
        print(f"Frames faltantes  : {len(missing)}")
        print(f"Chunks            : {len(capture.chunks)}/{capture.chunk_count}")
        print(f"CSI len           : {dict(lengths)}")
        print(f"first_word_invalid: {first_invalid}/{len(frames)}")
        if rssis:
            print(f"RSSI medio        : {mean(rssis):.2f} dBm")
            print(f"RSSI rango        : {min(rssis)} .. {max(rssis)} dBm")
        print(f"Vectores únicos   : {unique_vectors}/{len(frames)}")

        print()
        print("Validación señal CSI:")
        print(f"  estado            : {signal_health.status}")
        print(f"  signal-valid      : {signal_health.signal_valid}")
        print(f"  ratio únicos      : {signal_health.unique_ratio * 100:.2f} %")
        if signal_health.reason:
            print(f"  detalle           : {signal_health.reason}")
        if intervals_ms:
            print(f"Intervalo CSI med.: {mean(intervals_ms):.4f} ms")

        print()
        try:
            raw_diag = raw_csi_pair_diagnostics(frames)
            print("Auditoría buffer CSI crudo:")
            print(f"  modo LTF         : {raw_diag['ltf_mode']}")
            print(f"  CSI len          : {raw_diag['csi_len']} bytes")
            print(f"  frames CSI       : {raw_diag['frame_count']}")
            print(f"  pares I/Q crudos : {raw_diag['pair_count']}")
            print(
                "  pares siempre 0   : "
                f"{list(raw_diag['all_zero_pair_indices']) if raw_diag['all_zero_pair_indices'] else 'ninguno'}"
            )
            print(
                "  sc cero según docs: "
                f"{list(raw_diag['all_zero_documented_subcarriers']) if raw_diag['all_zero_documented_subcarriers'] else 'ninguno'}"
            )
            print(
                "  sc cero observado : "
                f"{list(raw_diag['all_zero_observed_subcarriers']) if raw_diag['all_zero_observed_subcarriers'] else 'ninguno'}"
            )
            print("  pares de control  :")
            for item in raw_diag["focus"]:
                print(
                    f"    pair={item['pair_index']:02d} "
                    f"doc_sc={item['documented_subcarrier']:+d} "
                    f"obs_sc={item['observed_subcarrier']:+d} "
                    f"zero={item['zero_fraction'] * 100:6.2f}% "
                    f"unique={item['unique_pairs']:3d} "
                    f"mean={item['mean_amplitude']:.3f} "
                    f"std={item['std_amplitude']:.3f}"
                )
            print()
        except CsiProcessingError as exc:
            print(f"[WARN] Auditoría CSI crudo no disponible: {exc}")
            print()

        accepted = False
        quality_status = "INVALID"

        if processed is not None:
            summary = block_amplitude_summary(processed)
            print("Procesamiento I/Q temporal:")
            print(f"  matriz fija      : {processed.shape[0]} x {processed.shape[1]}")
            if processed.shape[1] == 52:
                print("  subportadoras     : LLTF -26..-1, +1..+26")
            else:
                print("  subportadoras     : HT-LTF -28..-1, +1..+28")
            print(f"  cobertura raw     : {processed.coverage * 100:.2f} %")
            print(f"  muestras reales   : {sum(processed.valid_mask)}/{processed.target_frames}")
            print(f"  raw completo      : {processed.raw_complete}")
            print(f"  interpoladas      : {len(processed.interpolated_sequences)}")
            if processed.interpolated_sequences:
                print(f"  SEQ interpoladas  : {list(processed.interpolated_sequences)}")
            if processed.remaining_missing_sequences:
                print(f"  sin reconstruir   : {list(processed.remaining_missing_sequences)}")
            print(f"  model-ready       : {processed.model_ready}")
            print(f"  valores NaN final : {processed.nan_values}")
            if summary["mean"] is not None:
                print(f"  amplitud media    : {summary['mean']:.3f}")
                print(f"  amplitud rango    : {summary['min']:.3f} .. {summary['max']:.3f}")

            accepted = bool(
                signal_health.signal_valid
                and processed.model_ready
            )
            if accepted:
                quality_status = (
                    "ACCEPT_RAW"
                    if processed.raw_complete
                    else "ACCEPT_INTERPOLATED"
                )


            else:
                print()
                if not signal_health.signal_valid:
                    print(
                        f"[ERROR] {signal_health.status} detectado en "
                        f"{capture.device_id} block={capture.block_id}."
                    )
                else:
                    print(
                        f"[QUALITY] block={capture.block_id} de "
                        f"{capture.device_id} no es model-ready."
                    )
                print(
                    "          El intento queda pendiente de "
                    "DESCARTAR por el controller; no entra a "
                    "ventanas/baseline/inferencia."
                )
        else:
            print(f"[WARN] No se pudo decodificar I/Q: {processing_error}")

        print()
        print("Calidad del bloque:")
        print(f"  estado            : {quality_status}")
        print(f"  accepted          : {accepted}")
        print("  commit            : pendiente de decisión multi-RX")

        print()
        transport = self.udp_transport.get(capture.device_id, {})
        print("Transporte CSI2 UDP:")
        print(f"  UDP recibidos    : {transport.get('received', 0)}")
        print(f"  UDP perdidos     : {transport.get('lost', 0)}")
        print(f"  UDP duplicados   : {transport.get('duplicates', 0)}")
        print(f"  UDP out-of-order : {transport.get('out_of_order', 0)}")
        print(f"  UDP inválidos    : {self.udp_bad_packets}")

        print()
        print("Diagnóstico firmware:")
        print(f"  callbacks CSI   : {status.get('callbacks_total')}")
        print(f"  desde AP        : {status.get('source_matches')}")
        print(f"  probe WSEN      : {status.get('probe_matches')}")
        print(f"  duplicados      : {status.get('duplicates')}")
        print(f"  oversize        : {status.get('oversize')}")
        print(f"  inválidos       : {status.get('invalid')}")
        print(f"  queue_drop      : {status.get('queue_drop')}")
        print(f"  udp_csi_sent    : {status.get('udp_csi_sent')}")
        print(f"  udp_csi_errors  : {status.get('udp_csi_errors')}")
        print(f"  estimate_invalid: {status.get('invalid_estimate')}")
        if missing:
            print(f"SEQ faltantes     : {missing[:20]}")
        if signal_health.warning:
            print()
            print(f"[WARN] Sanidad CSI: {signal_health.status}.")
            if signal_health.reason:
                print(f"       {signal_health.reason}")
        print("======================================")

    def process_block_decision(self, raw: bytes):

        self.decision_queue.put(bytes(raw))

    def _decision_worker_loop(self):
        print("[LIVE-WORKER] Worker BLOCK_DECISION iniciado.")

        while True:
            raw = self.decision_queue.get()
            try:
                if raw is None:
                    return
                self._process_block_decision_sync(raw)
            except Exception as exc:
                print(f"[ERROR] Worker BLOCK_DECISION: {exc}")
            finally:
                self.decision_queue.task_done()

    def _start_decision_worker(self):
        if (
            self.decision_worker is not None
            and self.decision_worker.is_alive()
        ):
            return

        self.decision_worker = threading.Thread(
            target=self._decision_worker_loop,
            name="csi-decision-worker",
            daemon=True,
        )
        self.decision_worker.start()

    def _stop_decision_worker(self):
        if self.decision_worker is None:
            return

        if self.decision_worker.is_alive():
            self.decision_queue.put(None)
            self.decision_worker.join(timeout=5.0)


    def handle_ae_bucket(self, bucket: dict, *, config_id: int, session_id: str, block_id: int):
        if self.ae_capture_dir is None and self.ae_runtime is None:
            return
        x, extra = build_ae_block(bucket, self.ae_devices)

        if self.ae_capture_dir is not None:
            samples = self.ae_capture_dir / "samples"
            safe_session = str(session_id).replace("/", "_")
            out = samples / f"cfg{config_id}_sess{safe_session}_block{block_id:06d}.npz"
            tmp = out.with_suffix(".tmp.npz")
            np.savez_compressed(
                tmp, x=x.astype(np.float32),
                config_id=np.int64(config_id), block_id=np.int64(block_id),
                condition=np.array(self.ae_condition or "unknown"),
                session_id=np.array(self.ae_session_id),
                source_session_id=np.array(session_id),
            )
            tmp.replace(out)
            print(f"[AE-CAPTURE] block={block_id} shape={x.shape} file={out.name}", flush=True)

        if self.ae_runtime is not None:
            pred = self.ae_runtime.predict(x)
            payload = {
                "config_id": config_id, "session_id": session_id, "block_id": block_id,
                "mode": "oneclass_autoencoder_v1", "devices": list(self.ae_devices),
                "score_mean": pred.score_mean, "score_median": pred.score_median,
                "score_p95": pred.score_p95, "threshold": pred.threshold,
                "score_ratio": pred.ratio, "present": pred.present,
                "decision": "PRESENCE" if pred.present else "EMPTY",
                "input_shape": list(x.shape), **extra,
            }
            if getattr(pred, "branches", None):
                payload["branches"] = pred.branches
            print()
            print("======================================")
            print(" ONE-CLASS AUTOENCODER — LIVE")
            print("======================================")
            print(f"  block       : {block_id}")
            print(f"  score mean  : {pred.score_mean:.6f}")
            print(f"  score p95   : {pred.score_p95:.6f}")
            print(f"  threshold   : {pred.threshold:.6f}")
            print(f"  ratio       : {pred.ratio:.3f}x")
            if getattr(pred, "branches", None):
                bs = pred.branches.get("spectral", {})
                bd = pred.branches.get("delta", {})
                print(f"  spectral    : score={bs.get('score_mean',0):.6f} thr={bs.get('threshold',0):.6f} ratio={bs.get('ratio',0):.3f}x")
                print(f"  delta       : score={bd.get('score_mean',0):.6f} thr={bd.get('threshold',0):.6f} ratio={bd.get('ratio',0):.3f}x")
            print(f"  decisión    : {'PRESENCE' if pred.present else 'EMPTY'}")
            print("======================================")
            print("[AE-PRED] " + json.dumps(payload, separators=(",", ":")), flush=True)
            info = self.client.publish(
                f"{MQTT_BASE_TOPIC}/control/ae-result",
                json.dumps(payload, separators=(",", ":")), qos=1, retain=False,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                print(f"[WARN] MQTT ae-result rc={info.rc}")

    def _process_block_decision_sync(self, raw: bytes):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            print(f"[WARN] BLOCK_DECISION inválido: {exc}")
            return

        config_id = payload.get("config_id")
        session_id = payload.get("session_id")
        block_id = payload.get("block_id")
        action = payload.get("action")
        attempt = payload.get("attempt")
        devices = set(payload.get("devices") or [])

        success = True
        detail = None

        if (
            not isinstance(config_id, int)
            or not isinstance(session_id, str)
            or not isinstance(block_id, int)
            or action not in {"accept", "discard"}
            or not isinstance(attempt, int)
        ):
            return

        key = (config_id, session_id, block_id)

        try:
            if action == "accept":
                bucket = self.pending_experiment_blocks.pop(key, {})
                missing = devices - set(bucket) if devices else set()
                if missing:
                    raise RuntimeError(
                        "faltan bloques procesados de: "
                        + ", ".join(sorted(missing))
                    )

                self.handle_ae_bucket(
                    bucket, config_id=config_id, session_id=session_id, block_id=block_id
                )

                if self.calibration_output is not None:
                    self.commit_calibration_bucket(bucket)
                elif self._is_continuous_block(
                    config_id=config_id,
                    block_id=block_id,
                ):
                    self.commit_continuous_bucket(bucket)
                else:
                    for device_id in sorted(bucket):
                        item = bucket[device_id]
                        self.commit_processed_capture(
                            capture=item["capture"],
                            status=item["status"],
                            processed=item["processed"],
                            sensing=item["sensing"],
                        )

                print(
                    f"[COMMIT] block={block_id} ACCEPT: "
                    "captura multi-RX comprometida."
                )

            else:
                self.pending_experiment_blocks.pop(key, None)


                targets = devices or {
                    device_id
                    for (device_id, cfg_id, blk_id) in self.captures
                    if cfg_id == config_id and blk_id == block_id
                }
                for device_id in targets:
                    capture_key = (device_id, config_id, block_id)
                    self.captures.pop(capture_key, None)
                    self.pending_status.pop(capture_key, None)
                    self.signal_health.pop(capture_key, None)

                print(
                    f"[COMMIT] block={block_id} DISCARD: "
                    "intento multi-RX eliminado antes del reintento."
                )

        except Exception as exc:
            success = False
            detail = str(exc)
            print(
                f"[ERROR] Aplicando BLOCK_DECISION "
                f"{action} block={block_id}: {exc}"
            )

        ack = {
            "config_id": config_id,
            "session_id": session_id,
            "block_id": block_id,
            "action": action,
            "attempt": attempt,
            "success": success,
            "detail": detail,
        }
        self.client.publish(
            f"{MQTT_BASE_TOPIC}/control/block-decision-ack",
            json.dumps(ack, separators=(",", ":")),
            qos=1,
            retain=False,
        )

    def commit_calibration_bucket(self, bucket: dict):
        if self.calibration_output is None:
            raise RuntimeError("calibration_output no configurado")
        if not bucket:
            raise RuntimeError("bucket vacío en calibración")

        missing = [
            dev for dev in self.espectre_devices
            if dev not in bucket
        ]
        if missing:
            raise RuntimeError(
                "faltan receptores para baseline: "
                + ", ".join(missing)
            )

        matrices = []
        source_lengths_by_device = {}
        for device_id in self.espectre_devices:
            item = bucket[device_id]
            processed = item["processed"]
            capture = item["capture"]
            source_lengths = sorted({
                len(frame.csi)
                for frame in capture.frames.values()
            })
            source_lengths_by_device[device_id] = source_lengths
            if source_lengths != [LLTF_CSI_LEN]:
                raise RuntimeError(
                    f"{device_id}: calibración LLTF requiere CSI de "
                    f"{LLTF_CSI_LEN} bytes; recibido={source_lengths}. "
                    "Flashea primero el firmware LLTF en los 3 RX."
                )

            matrix = np.asarray(
                getattr(processed, "espectre_amplitude_matrix", None),
                dtype=np.float32,
            )
            if matrix.shape != (400, 56):
                raise RuntimeError(
                    f"{device_id}: ESPectre56 shape={matrix.shape}; esperado=(400,56)"
                )
            if not np.isfinite(matrix).all():
                raise RuntimeError(
                    f"{device_id}: ESPectre56 contiene NaN/Inf"
                )
            matrices.append(matrix)

        stacked = np.stack(matrices, axis=1)
        sample_idx = np.arange(0, 400, 10, dtype=np.int64)
        sampled = stacked[sample_idx].reshape(40, 168)

        norms = np.linalg.norm(sampled, axis=1, keepdims=True)
        norms[norms < 1e-6] = 1.0
        normalized = sampled / norms

        self.calibration_rows.append(normalized.astype(np.float32))
        self.calibration_accepted_blocks += 1

        sample = next(iter(bucket.values()))
        capture = sample["capture"]
        sensing = sample["sensing"]
        status = sample["status"]

        print(
            f"[CALIBRATION] block={capture.block_id} "
            f"aceptado {self.calibration_accepted_blocks}/"
            f"{self.calibration_target_blocks}; "
            f"muestras={self.calibration_accepted_blocks * 40}"
        )

        if self.calibration_accepted_blocks >= self.calibration_target_blocks:
            rows = np.concatenate(self.calibration_rows, axis=0)
            if rows.shape != (self.calibration_target_blocks * 40, 168):
                raise RuntimeError(
                    f"shape calibración inesperado: {rows.shape}"
                )

            baseline_flat = rows.mean(axis=0).astype(np.float32)
            baseline = baseline_flat.reshape(3, 56)

            output = self.calibration_output
            output.parent.mkdir(parents=True, exist_ok=True)
            tmp = output.with_name(output.name + ".tmp.npy")
            np.save(tmp, baseline)
            tmp.replace(output)

            metadata_path = output.parent / "receiver_metadata.json"
            metadata = {
                "technical": {
                    "method": "mean_l2_normalized_espectre56_3rx",
                    "input_contract": "s3_lltf_first56_reconstructed",
                    "c5_source_ltf": "LLTF",
                    "c5_source_csi_len": LLTF_CSI_LEN,
                    "c5_source_complex": LLTF_CSI_LEN // 2,
                    "s3_reconstructed_layout": "0..31,-32..-1",
                    "s3_model_feature_positions": [0, 55],
                    "device_order": list(self.espectre_devices),
                    "source_frames_per_block": 400,
                    "source_rate_hz": 200,
                    "subsample_step": 10,
                    "effective_rate_hz": 20,
                    "effective_samples_per_block": 40,
                    "baseline_shape": [3, 56],
                },
                "channel": sensing.get("expected_channel"),
                "bandwidth_mhz": sensing.get("bandwidth_mhz"),
                "phy": sensing.get("phy"),
                "band": sensing.get("band"),
                "session_id": status.get("session_id"),
                "config_id": capture.config_id,
                "accepted_blocks": self.calibration_accepted_blocks,
                "effective_samples": int(rows.shape[0]),
            }
            metadata_path.write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            print()
            print("======================================")
            print(" BASELINE LOCAL ESPectre CREADO")
            print("======================================")
            print(f"Nombre            : {self.calibration_name}")
            print(f"Banda             : {sensing.get('band')}")
            print(f"Canal             : {sensing.get('expected_channel')}")
            print(f"BW                : {sensing.get('bandwidth_mhz')} MHz")
            print(f"Bloques aceptados : {self.calibration_accepted_blocks}")
            print(f"Muestras efectivas: {rows.shape[0]}")
            print(f"Shape baseline    : {baseline.shape}")
            print(f"Archivo           : {output}")
            print("======================================")

    def _is_continuous_block(
        self,
        *,
        config_id: int,
        block_id: int,
    ) -> bool:
        config = self.configs.get(config_id, {})
        capture_cfg = config.get("capture", {})

        try:
            configured_blocks = int(
                capture_cfg.get("blocks", 0) or 0
            )
        except (TypeError, ValueError):
            configured_blocks = 0

        formal_limit = configured_blocks if configured_blocks > 0 else 4
        return int(block_id) > formal_limit

    def _cleanup_continuous_state(
        self,
        *,
        config_id: int,
        session_id: str,
        block_id: int,
    ):
        for key in list(self.captures):
            _device, cfg_id, blk_id = key
            if cfg_id == config_id and blk_id <= block_id:
                self.captures.pop(key, None)
                self.pending_status.pop(key, None)
                self.signal_health.pop(key, None)

        for key in list(self.processed_blocks):
            _device, cfg_id, sess_id = key
            if cfg_id == config_id and sess_id == session_id:
                self.processed_blocks.pop(key, None)

        for key in list(self.processed_windows):
            _device, cfg_id, sess_id, _window = key
            if cfg_id == config_id and sess_id == session_id:
                self.processed_windows.pop(key, None)

        for key in list(self.espectre_pending_blocks):
            cfg_id, sess_id, blk_id = key
            if (
                cfg_id == config_id
                and sess_id == session_id
                and blk_id <= block_id
            ):
                self.espectre_pending_blocks.pop(key, None)
                self.espectre_reported.discard(key)
                self.espectre_invalid_blocks.pop(key, None)

        print(
            f"[LIVE-CLEANUP] block={block_id}: "
            "estado temporal liberado."
        )

    def commit_continuous_bucket(self, bucket: dict):
        if not bucket:
            raise RuntimeError("bucket vacío en commit live")

        sample = next(iter(bucket.values()))
        capture = sample["capture"]
        status = sample["status"]

        config_id = capture.config_id
        block_id = capture.block_id
        session_id = status.get("session_id") or "unknown"

        print(
            f"[LIVE-COMMIT] block={block_id}: "
            "ruta liviana Zero-shot A."
        )

        for device_id in sorted(bucket):
            item = bucket[device_id]
            self.register_espectre_block(
                capture=item["capture"],
                status=item["status"],
                processed=item["processed"],
            )

        self._cleanup_continuous_state(
            config_id=config_id,
            session_id=session_id,
            block_id=block_id,
        )

    def commit_processed_capture(
        self,
        *,
        capture: Capture,
        status: dict,
        processed,
        sensing: dict,
    ):
        window, window_error = self.register_processed_block(
            capture=capture,
            status=status,
            processed=processed,
        )

        self.register_espectre_block(
            capture=capture,
            status=status,
            processed=processed,
        )

        if window_error is not None:
            print()
            print(
                f"[WARN] No se pudo formar ventana temporal: "
                f"{window_error}"
            )

        if window is None:
            return

        summary = window_amplitude_summary(window)
        print()
        print("Ventana temporal para modelo:")
        print(f"  ventana           : {window.window_index}")
        print(f"  bloques           : {window.block_ids[0]} + {window.block_ids[1]}")
        print(f"  matriz amplitud   : {window.shape[0]} x {window.shape[1]}")
        print(f"  cobertura raw     : {window.raw_coverage * 100:.2f} %")
        print(f"  interpoladas      : {window.interpolated_samples}")
        print(f"  valores NaN       : {window.nan_values}")
        print(f"  model-ready       : {window.model_ready}")
        if summary["mean"] is not None:
            print(f"  amplitud media    : {summary['mean']:.3f}")

        try:
            diagnostics = window_subcarrier_diagnostics(window)
            print()
            print("Caracterización por subportadora:")
            print(
                f"  ceros amplitud    : {diagnostics['total_zero_values']}/"
                f"{diagnostics['total_finite_values']} "
                f"({diagnostics['zero_fraction'] * 100:.4f} %)"
            )
            print(
                f"  std temporal      : min={diagnostics['std_min']:.4f}, "
                f"mediana={diagnostics['std_median']:.4f}, "
                f"max={diagnostics['std_max']:.4f}"
            )
            constants = list(diagnostics["constant_subcarriers"])
            print(
                f"  subp. constantes  : "
                f"{constants if constants else 'ninguna'}"
            )
            least = diagnostics["least_variable"]
            most = diagnostics["most_variable"]
            print(
                "  menos variables   : "
                + ", ".join(
                    f"{item['subcarrier']:+d}(std={item['std']:.3f})"
                    for item in least
                )
            )
            print(
                "  más variables     : "
                + ", ".join(
                    f"{item['subcarrier']:+d}(std={item['std']:.3f})"
                    for item in most
                )
            )
            pilots = diagnostics["pilot_stats"]
            pilot_order = (-21, -7, 7, 21)
            print(
                "  pilotos            : "
                + ", ".join(
                    f"{index:+d}(mean={pilots[index]['mean']:.2f}, "
                    f"std={pilots[index]['std']:.3f}, "
                    f"zero={pilots[index]['zero_fraction'] * 100:.2f}%)"
                    for index in pilot_order
                    if index in pilots
                )
            )
        except CsiProcessingError as exc:
            print(
                f"[WARN] Caracterización no disponible: {exc}"
            )

        self.handle_baseline_and_normalization(
            capture=capture,
            sensing=sensing,
            window=window,
        )

    def invalidate_espectre_block(
        self,
        *,
        device_id: str,
        config_id: int,
        session_id: str,
        block_id: int,
        reason: str,
    ):


        key = (
            config_id,
            session_id,
            block_id,
        )

        self.espectre_invalid_blocks[key] = {
            "device_id": device_id,
            "reason": reason,
        }


        self.espectre_pending_blocks.pop(
            key,
            None,
        )

    def register_espectre_block(
        self,
        *,
        capture: Capture,
        status: dict,
        processed,
    ):
        if self.espectre is None:
            return None

        if capture.device_id not in self.espectre_devices:
            return None

        session_id = status.get("session_id") or "unknown"

        key = (
            capture.config_id,
            session_id,
            processed.block_id,
        )

        invalid = self.espectre_invalid_blocks.get(key)
        if invalid is not None:
            print()
            print("ESPectre Sense zero-shot:")
            print(
                f"  block             : {processed.block_id}"
            )
            print(
                "  estado            : BLOQUEADO"
            )
            print(
                f"  motivo            : {invalid['device_id']} / "
                f"{invalid['reason']}"
            )
            return None

        bucket = self.espectre_pending_blocks.setdefault(
            key,
            {},
        )
        bucket[capture.device_id] = processed

        if key in self.espectre_reported:
            return None

        missing = [
            dev
            for dev in self.espectre_devices
            if dev not in bucket
        ]

        if missing:
            print()
            print("ESPectre Sense zero-shot:")
            print(
                f"  block             : {processed.block_id}"
            )
            print(
                "  esperando          : "
                + ", ".join(missing)
            )
            return None

        try:
            prediction = self.espectre.predict_blocks(
                bucket
            )
        except EspectreModelError as exc:
            print()
            print(
                f"[WARN] ESPectre zero-shot block={processed.block_id}: {exc}"
            )
            return None

        self.espectre_reported.add(key)

        probs = prediction.probability_map()

        print()
        print("======================================")
        print(" ESPectre Sense — ZERO-SHOT")
        print("======================================")
        print(
            "  pesos             : preentrenados / congelados"
        )
        print(
            "  receptores        : "
            + " + ".join(self.espectre_devices)
        )
        print(
            f"  block             : {processed.block_id}"
        )
        print(
            f"  input adaptado    : {prediction.input_shape[0]} x {prediction.input_shape[1]}"
        )
        print(
            "  muestreo          : 200 Hz -> 20 Hz (1 de cada 10)"
        )
        print(
            "  clases            : "
            + ", ".join(prediction.classes)
        )

        for name in prediction.classes:
            print(
                f"  P({name:<8})      : {probs[name] * 100:6.2f} %"
            )

        print(
            f"  clase predicha    : {prediction.predicted_class}"
        )
        print(
            f"  confianza clase   : {prediction.predicted_probability * 100:.2f} %"
        )
        print(
            f"  P(presencia)      : {prediction.presence_probability * 100:.2f} %"
        )
        print(
            f"  presencia binaria : {'SI' if prediction.present else 'NO'}"
        )
        print("======================================")


        result_payload = {
            "config_id": capture.config_id,
            "session_id": status.get("session_id") or "unknown",
            "block_id": processed.block_id,
            "mode": "zero_shot_a",
            "devices": list(self.espectre_devices),
            "classes": list(prediction.classes),
            "probabilities": probs,
            "predicted_class": prediction.predicted_class,
            "predicted_probability": prediction.predicted_probability,
            "presence_probability": prediction.presence_probability,
            "present": prediction.present,
            "input_shape": list(prediction.input_shape),
            "sampled_sequences": list(prediction.sampled_sequences),
        }


        print(
            "[ESPECTRE-PRED] "
            + json.dumps(result_payload, separators=(",", ":")),
            flush=True,
        )

        info = self.client.publish(
            f"{MQTT_BASE_TOPIC}/control/espectre-result",
            json.dumps(result_payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            print(
                "[WARN] No se pudo publicar resultado ESPectre "
                f"en MQTT: rc={info.rc}"
            )

        return prediction

    def register_processed_block(
        self,
        *,
        capture: Capture,
        status: dict,
        processed,
    ):
        session_id = status.get("session_id") or "unknown"

        session_key = (
            capture.device_id,
            capture.config_id,
            session_id,
        )

        blocks = self.processed_blocks.setdefault(
            session_key,
            {},
        )

        blocks[processed.block_id] = processed


        if processed.block_id % 2 != 0:
            return None, None

        first_id = processed.block_id - 1
        first = blocks.get(first_id)

        if first is None:
            return (
                None,
                f"falta block={first_id} para emparejar con block={processed.block_id}",
            )

        try:
            window = build_window(
                first=first,
                second=processed,
            )
        except CsiProcessingError as exc:
            return None, str(exc)

        window_key = (
            capture.device_id,
            capture.config_id,
            session_id,
            window.window_index,
        )

        self.processed_windows[window_key] = window

        return window, None

    def handle_baseline_and_normalization(
        self,
        *,
        capture: Capture,
        sensing: dict,
        window,
    ):

        band = sensing.get("band")
        channel = sensing.get("expected_channel")
        bandwidth = sensing.get("bandwidth_mhz")

        baseline_key = (
            capture.device_id,
            band,
            channel,
            bandwidth,
        )

        baseline = self.baselines.get(baseline_key)

        if baseline is None:
            if self.baseline_windows_required <= 0:
                return None

            if not window.model_ready:
                print()
                print("Baseline vacío:")
                print("  ventana ignorada  : no es model-ready")
                return None

            buffer = self.baseline_buffers.setdefault(
                baseline_key,
                [],
            )
            buffer.append(window)

            print()
            print("Baseline vacío:")
            print(
                f"  clave             : {capture.device_id} / {band} / ch{channel} / {bandwidth}MHz"
            )
            print(
                f"  progreso          : {len(buffer)}/{self.baseline_windows_required} ventanas"
            )
            print("  uso               : aprendizaje; no se usa como muestra objetivo")

            if len(buffer) < self.baseline_windows_required:
                return None

            try:
                baseline = build_baseline_profile(
                    buffer[:self.baseline_windows_required]
                )
            except CsiProcessingError as exc:
                print(f"[WARN] No se pudo construir baseline: {exc}")
                return None
            self.baselines[baseline_key] = baseline
            del self.baseline_buffers[baseline_key]

            refs = baseline.reference_amplitude
            print("  estado            : BASELINE LISTO")
            print(
                f"  muestras          : {baseline.source_samples} por {len(refs)} subportadoras"
            )
            print(
                f"  referencia amp.   : media={mean(refs):.3f}, min={min(refs):.3f}, max={max(refs):.3f}"
            )
            print(
                "  siguiente captura : será normalizada contra esta referencia"
            )
            return None

        try:
            normalized = normalize_window_to_baseline(
                window,
                baseline,
            )
        except CsiProcessingError as exc:
            print()
            print(f"[WARN] Normalización baseline falló: {exc}")
            return None

        summary = normalized_window_summary(normalized)

        print()
        print("Normalización contra baseline vacío:")
        print(
            f"  clave             : {capture.device_id} / {band} / ch{channel} / {bandwidth}MHz"
        )
        print(
            f"  matriz delta dB   : {normalized.shape[0]} x {normalized.shape[1]}"
        )
        print(
            f"  model-ready       : {normalized.model_ready}"
        )
        print(
            f"  valores NaN       : {normalized.nan_values}"
        )

        if summary["mean_db"] is not None:
            print(
                f"  delta medio       : {summary['mean_db']:+.3f} dB"
            )
            print(
                f"  dispersión        : {summary['std_db']:.3f} dB"
            )
            print(
                f"  rango             : {summary['min_db']:+.3f} .. {summary['max_db']:+.3f} dB"
            )

        return normalized

    def run(self):
        self.udp_stop.clear()
        self.udp_thread = threading.Thread(
            target=self.udp_loop,
            name="csi2-udp",
            daemon=True,
        )
        self.udp_thread.start()

        self.client.connect(
            MQTT_HOST,
            MQTT_PORT,
            keepalive=30,
        )

        self.client.loop_start()
        self._start_decision_worker()

        try:
            if self.once:
                self.done.wait()
            else:
                while True:
                    self.done.wait(3600)
        except KeyboardInterrupt:
            print("\n[CSI] Interrumpido.")
        finally:
            self.udp_stop.set()
            self._stop_decision_worker()
            self.client.disconnect()
            self.client.loop_stop()
            if self.udp_thread is not None:
                self.udp_thread.join(timeout=1.5)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--baseline-windows",
        type=int,
        default=0,
        help=(
            "Aprender un baseline vacío en memoria usando las primeras N "
            "ventanas model-ready de cada receptor/banda/canal/BW. "
            "Mantener el proceso abierto para normalizar capturas posteriores."
        ),
    )

    parser.add_argument(
        "--espectre-model-dir",
        default=None,
        help=(
            "Directorio con csi_light_weights.json, baseline.npy, "
            "feat_mean.npy y feat_std.npy de ESPectre Sense. "
            "Activa inferencia zero-shot con pesos congelados."
        ),
    )

    parser.add_argument(
        "--espectre-devices",
        default="rx01,rx02,rx03",
        help=(
            "Orden de receptores para las 3 entradas del modelo "
            "(default: rx01,rx02,rx03)."
        ),
    )

    parser.add_argument(
        "--espectre-baseline",
        default=None,
        help="Baseline local .npy para sobrescribir baseline.npy publicado.",
    )

    parser.add_argument(
        "--calibration-output",
        default=None,
        help="Ruta baseline.npy; activa modo de creación de baseline local.",
    )

    parser.add_argument(
        "--calibration-name",
        default="baseline",
        help="Nombre descriptivo del baseline local.",
    )

    parser.add_argument(
        "--calibration-target-blocks",
        type=int,
        default=13,
        help="Bloques EMPTY válidos requeridos (default 13 = ~26 s).",
    )


    parser.add_argument("--ae-capture-dir", default=None, help="Directorio de sesión para guardar bloques AE 40x156.")
    parser.add_argument("--ae-condition", default=None, choices=["empty", "static", "movement"], help="Etiqueta de captura AE.")
    parser.add_argument("--ae-session-id", default=None, help="ID lógico de sesión AE.")
    parser.add_argument("--ae-model-dir", default=None, help="Directorio del modelo one-class AE para inferencia live.")
    parser.add_argument("--ae-devices", default="rx01,rx02,rx03", help="Orden de RX para AE.")

    parser.add_argument(
        "--once",
        action="store_true",
        help="Salir después de reconstruir un bloque CSI.",
    )

    args = parser.parse_args()

    if args.baseline_windows < 0:
        parser.error("--baseline-windows debe ser >= 0")
    if args.calibration_target_blocks < 1 or args.calibration_target_blocks > 120:
        parser.error("--calibration-target-blocks debe estar entre 1 y 120")

    devices = tuple(
        item.strip()
        for item in args.espectre_devices.split(",")
        if item.strip()
    )

    if args.espectre_model_dir and len(devices) != 3:
        parser.error("--espectre-devices debe contener exactamente 3 receptores")

    ae_devices = tuple(x.strip() for x in args.ae_devices.split(",") if x.strip())
    if (args.ae_capture_dir or args.ae_model_dir) and len(ae_devices) != 3:
        parser.error("--ae-devices debe contener exactamente 3 receptores")
    if args.ae_capture_dir and not args.ae_condition:
        parser.error("--ae-capture-dir requiere --ae-condition")

    receiver = CsiReceiver(
        once=args.once,
        baseline_windows=args.baseline_windows,
        espectre_model_dir=args.espectre_model_dir,
        espectre_devices=devices,
        espectre_baseline=args.espectre_baseline,
        calibration_output=args.calibration_output,
        calibration_name=args.calibration_name,
        calibration_target_blocks=args.calibration_target_blocks,
        ae_capture_dir=args.ae_capture_dir,
        ae_condition=args.ae_condition,
        ae_session_id=args.ae_session_id,
        ae_model_dir=args.ae_model_dir,
        ae_devices=ae_devices,
    )

    receiver.run()


if __name__ == "__main__":
    main()
