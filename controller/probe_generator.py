#!/usr/bin/env python3
import argparse, json, os, socket, struct, time
from pathlib import Path
from statistics import mean, pstdev

from settings import UDP_TARGET_PORT, UDP_BROADCAST

UDP_TARGET_STAGGER_MS = float(
    os.getenv("UDP_TARGET_STAGGER_MS", "0").strip() or "0"
)

MAGIC = b"WSEN"
PROTOCOL_VERSION = 1
PACKET_FORMAT = "!4sB3xQIIQ"
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)

class ProbeGenerator:
    def __init__(
        self,
        target_host: str,
        target_port: int,
        broadcast: bool,
        stagger_ms: float | None = None,
    ):
        self.target_hosts = tuple(
            x.strip() for x in str(target_host).split(",") if x.strip()
        )
        if not self.target_hosts:
            raise ValueError("No hay destinos UDP configurados")
        if len(set(self.target_hosts)) != len(self.target_hosts):
            raise ValueError(f"Destinos UDP duplicados: {self.target_hosts}")

        self.target_host = ",".join(self.target_hosts)
        self.target_port = int(target_port)
        self.broadcast = bool(broadcast)

        if stagger_ms is None:
            stagger_ms = UDP_TARGET_STAGGER_MS

        self.stagger_ms = float(stagger_ms)

        if self.stagger_ms < 0:
            raise ValueError("stagger_ms debe ser >= 0")

        if self.broadcast and len(self.target_hosts) > 1:
            raise ValueError(
                "Con varios receptores usar UDP_BROADCAST=false"
            )

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.broadcast:
            self.sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_BROADCAST, 1
            )

    def build_packet(self, config_id, block_id, sequence, timestamp_ns):
        return struct.pack(
            PACKET_FORMAT,
            MAGIC,
            PROTOCOL_VERSION,
            config_id,
            block_id,
            sequence,
            timestamp_ns,
        )

    def run_block(self, config_id, block_id, rate_hz, packet_count):
        if rate_hz <= 0:
            raise ValueError("rate_hz debe ser > 0")
        if packet_count <= 0:
            raise ValueError("packet_count debe ser > 0")

        period_ns = int(1_000_000_000 / rate_hz)

        max_offset_ms = (
            (len(self.target_hosts) - 1)
            * self.stagger_ms
        )
        period_ms = 1000.0 / rate_hz

        if max_offset_ms >= period_ms:
            raise ValueError(
                "El stagger total debe ser menor que el período. "
                f"stagger_total={max_offset_ms:.3f} ms, "
                f"periodo={period_ms:.3f} ms"
            )

        intervals_ms = []
        fanout_us = []

        print()
        print("======================================")
        print(" UDP PROBE GENERATOR MULTI-TARGET")
        print("======================================")
        print(f"Destinos          : {len(self.target_hosts)}")
        for i, host in enumerate(self.target_hosts, 1):
            print(f"  [{i}]              : {host}:{self.target_port}")
        print(f"Broadcast         : {self.broadcast}")
        print(f"Stagger           : {self.stagger_ms:.3f} ms entre receptores")
        for i, host in enumerate(self.target_hosts):
            print(
                f"  offset {host:<15}: "
                f"{i * self.stagger_ms:.3f} ms"
            )
        print(f"Config ID         : {config_id}")
        print(f"Block ID          : {block_id}")
        print(f"Tasa por receptor : {rate_hz} Hz")
        print(f"Periodo lógico    : {period_ns / 1_000_000:.3f} ms")
        print(f"Sequences         : {packet_count}")
        print(
            f"Datagramas totales: "
            f"{packet_count * len(self.target_hosts)}"
        )
        print(f"Tamaño payload    : {PACKET_SIZE} bytes")
        print()

        start_ns = time.monotonic_ns()
        previous_batch_ns = None
        last_batch_ns = start_ns

        for seq in range(packet_count):
            deadline_ns = start_ns + seq * period_ns

            while True:
                now_ns = time.monotonic_ns()
                remaining_ns = deadline_ns - now_ns
                if remaining_ns <= 0:
                    break
                if remaining_ns > 500_000:
                    time.sleep((remaining_ns - 500_000) / 1_000_000_000)

            batch_start_ns = time.monotonic_ns()

            packet = self.build_packet(
                config_id,
                block_id,
                seq,
                batch_start_ns,
            )

            first_send_ns = None
            last_send_ns = None

            for target_index, host in enumerate(self.target_hosts):


                target_offset_ns = int(
                    target_index
                    * self.stagger_ms
                    * 1_000_000
                )
                target_deadline_ns = (
                    deadline_ns
                    + target_offset_ns
                )

                while True:
                    now_ns = time.monotonic_ns()
                    remaining_ns = target_deadline_ns - now_ns

                    if remaining_ns <= 0:
                        break

                    if remaining_ns > 500_000:
                        time.sleep(
                            (remaining_ns - 500_000)
                            / 1_000_000_000
                        )

                if first_send_ns is None:
                    first_send_ns = time.monotonic_ns()

                sent = self.sock.sendto(
                    packet,
                    (host, self.target_port),
                )

                if sent != PACKET_SIZE:
                    raise RuntimeError(
                        f"{host}: {sent}/{PACKET_SIZE} bytes enviados"
                    )

                last_send_ns = time.monotonic_ns()

            if first_send_ns is not None and last_send_ns is not None:
                fanout_us.append(
                    (last_send_ns - first_send_ns) / 1000.0
                )

            if previous_batch_ns is not None:
                intervals_ms.append(
                    (batch_start_ns - previous_batch_ns) / 1_000_000.0
                )

            previous_batch_ns = batch_start_ns
            last_batch_ns = batch_start_ns

        elapsed_s = (last_batch_ns - start_ns) / 1_000_000_000.0
        effective_rate = (
            (packet_count - 1) / elapsed_s
            if packet_count > 1 and elapsed_s > 0
            else 0.0
        )

        print()
        print("======================================")
        print(" RESULTADOS DEL GENERADOR")
        print("======================================")
        print(f"Duración real      : {elapsed_s:.6f} s")
        print(f"Tasa efectiva      : {effective_rate:.3f} Hz/receptor")

        if intervals_ms:
            expected_ms = 1000.0 / rate_hz
            avg = mean(intervals_ms)
            std = pstdev(intervals_ms) if len(intervals_ms) > 1 else 0.0
            print(f"Intervalo medio    : {avg:.4f} ms")
            print(f"Desv. estándar     : {std:.4f} ms")
            print(f"Intervalo mínimo   : {min(intervals_ms):.4f} ms")
            print(f"Intervalo máximo   : {max(intervals_ms):.4f} ms")
            print(f"Error medio        : {abs(avg - expected_ms):.4f} ms")

        if fanout_us:
            print(f"Fan-out medio      : {mean(fanout_us):.1f} us")
            print(f"Fan-out máximo     : {max(fanout_us):.1f} us")

        print("======================================")

        return {
            "elapsed_s": elapsed_s,
            "effective_rate_hz": effective_rate,
            "target_hosts": list(self.target_hosts),
            "fanout_mean_us": mean(fanout_us) if fanout_us else None,
            "fanout_max_us": max(fanout_us) if fanout_us else None,
        }

def load_config():
    path = Path(__file__).resolve().parent / "experiment.json"
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-id", type=int, required=True)
    parser.add_argument("--block-id", type=int, default=1)

    parser.add_argument(
        "--targets",
        required=True,
        help="IPs destino separadas por coma",
    )

    args = parser.parse_args()

    cfg = load_config()

    generator = ProbeGenerator(
        target_host=args.targets,
        target_port=UDP_TARGET_PORT,
        broadcast=UDP_BROADCAST,
        stagger_ms=UDP_TARGET_STAGGER_MS,
    )

    generator.run_block(
        config_id=args.config_id,
        block_id=args.block_id,
        rate_hz=cfg["probe"]["rate_hz"],
        packet_count=cfg["capture"]["target_packets"],
    )

if __name__ == "__main__":
    main()
