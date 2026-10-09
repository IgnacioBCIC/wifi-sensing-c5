#!/usr/bin/env python3

"""
Uso recomendado:
    cd ~/wifi-sensing
    .venv/bin/python scripts/status_rx.py

Opciones:
    .venv/bin/python scripts/status_rx.py --devices rx01
    .venv/bin/python scripts/status_rx.py --devices rx01,rx03
    .venv/bin/python scripts/status_rx.py --timeout 3
    .venv/bin/python scripts/status_rx.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    import paho.mqtt.client as mqtt
    from dotenv import dotenv_values
except ImportError as exc:
    raise SystemExit(
        "Falta una dependencia Python. Ejecuta el script con:\n"
        "  .venv/bin/python scripts/status_rx.py\n"
        f"Detalle: {exc}"
    )


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def env_required(env: dict[str, Any], key: str) -> str:
    value = str(env.get(key, "")).strip()
    if not value:
        raise SystemExit(f"Falta {key} en {ENV_FILE}")
    return value


def parse_devices(raw: str | None, env: dict[str, Any]) -> list[str]:
    if raw:
        devices = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        configured = str(env.get("RX_DEVICES", "")).strip()
        devices = [x.strip() for x in configured.split(",") if x.strip()]
        if not devices:
            devices = ["rx01", "rx02", "rx03"]

    return list(dict.fromkeys(devices))


def first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def format_uptime(ms: Any) -> str:
    try:
        seconds = int(ms) // 1000
    except (TypeError, ValueError):
        return "-"

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def band_from_channel(channel: Any) -> str:
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return "-"
    if 1 <= ch <= 14:
        return "2.4g"
    if ch > 14:
        return "5g"
    return "-"


def ping_ms(ip: str | None) -> float | None:
    if not ip:
        return None

    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if proc.returncode != 0:
        return None

    match = re.search(r"time[=<]([\d.]+)\s*ms", proc.stdout)
    if not match:
        return None

    try:
        return float(match.group(1))
    except ValueError:
        return None


def run_text(command: list[str]) -> str:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""

    if proc.returncode != 0:
        return ""
    return proc.stdout


def get_ap_info(interface: str) -> dict[str, Any]:
    output = run_text(["iw", "dev", interface, "info"])
    result: dict[str, Any] = {
        "interface": interface,
        "ssid": None,
        "channel": None,
        "frequency_mhz": None,
        "width_mhz": None,
        "txpower_dbm": None,
    }

    if not output:
        return result

    match = re.search(r"ssid\s+(.+)", output, re.IGNORECASE)
    if match:
        result["ssid"] = match.group(1).strip()

    match = re.search(
        r"channel\s+(\d+)\s+\((\d+)\s+MHz\).*?width:\s*([0-9]+)\s*MHz",
        output,
        re.IGNORECASE,
    )
    if match:
        result["channel"] = int(match.group(1))
        result["frequency_mhz"] = int(match.group(2))
        result["width_mhz"] = int(match.group(3))
    else:
        match = re.search(r"channel\s+(\d+)\s+\((\d+)\s+MHz\)", output)
        if match:
            result["channel"] = int(match.group(1))
            result["frequency_mhz"] = int(match.group(2))

    match = re.search(r"txpower\s+([-\d.]+)\s+dBm", output, re.IGNORECASE)
    if match:
        try:
            result["txpower_dbm"] = float(match.group(1))
        except ValueError:
            pass

    return result


def get_station_info(interface: str) -> dict[str, dict[str, Any]]:
    output = run_text(["iw", "dev", interface, "station", "dump"])
    stations: dict[str, dict[str, Any]] = {}

    if not output:
        return stations

    current_mac: str | None = None

    for raw_line in output.splitlines():
        line = raw_line.strip()

        match = re.match(r"Station\s+([0-9a-fA-F:]{17})", line)
        if match:
            current_mac = match.group(1).lower()
            stations[current_mac] = {}
            continue

        if current_mac is None:
            continue

        data = stations[current_mac]

        match = re.match(r"signal:\s*(-?\d+)", line)
        if match:
            data["signal_dbm"] = int(match.group(1))
            continue

        match = re.match(r"inactive time:\s*(\d+)\s*ms", line)
        if match:
            data["inactive_ms"] = int(match.group(1))
            continue

        match = re.match(r"tx bitrate:\s*([\d.]+)\s*MBit/s", line)
        if match:
            data["tx_mbps"] = float(match.group(1))
            continue

        match = re.match(r"rx bitrate:\s*([\d.]+)\s*MBit/s", line)
        if match:
            data["rx_mbps"] = float(match.group(1))
            continue

    return stations


class MqttSnapshot:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        base_topic: str,
        devices: list[str],
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.base_topic = base_topic.rstrip("/")
        self.devices = devices

        self.connected = threading.Event()
        self.lock = threading.Lock()

        self.info: dict[str, dict[str, Any]] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self.heartbeat: dict[str, dict[str, Any]] = {}

        client_id = f"wifi-sensing-status-{os.getpid()}"

        try:
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
            self.api_v2 = True
        except AttributeError:
            self.client = mqtt.Client(
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
            self.api_v2 = False

        self.client.username_pw_set(user, password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        is_failure = False

        if hasattr(reason_code, "is_failure"):
            is_failure = bool(reason_code.is_failure)
        else:
            try:
                rc = int(getattr(reason_code, "value", reason_code))
                is_failure = rc != 0
            except (TypeError, ValueError):
                is_failure = True

        if is_failure:
            return

        client.subscribe(
            [
                (f"{self.base_topic}/devices/+/info", 1),
                (f"{self.base_topic}/devices/+/status", 1),
            ]
        )
        self.connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        if not isinstance(payload, dict):
            return

        parts = msg.topic.split("/")
        try:
            idx = parts.index("devices")
            device = parts[idx + 1]
            kind = parts[idx + 2]
        except (ValueError, IndexError):
            return

        if device not in self.devices:
            return

        with self.lock:
            if kind == "info":
                self.info[device] = payload
            elif kind == "status":
                self.status[device] = payload
                if str(payload.get("state", "")).upper() == "HEARTBEAT":
                    self.heartbeat[device] = payload

    def collect(self, timeout: float) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()

        try:
            if not self.connected.wait(min(timeout, 5.0)):
                raise RuntimeError(
                    f"No se pudo conectar/suscribir a MQTT {self.host}:{self.port}"
                )

            deadline = time.monotonic() + timeout

            while time.monotonic() < deadline:
                with self.lock:
                    have_info = all(d in self.info for d in self.devices)
                    have_hb = all(d in self.heartbeat for d in self.devices)

                if have_info and have_hb:
                    break

                time.sleep(0.1)
        finally:
            try:
                self.client.disconnect()
            finally:
                self.client.loop_stop()


def build_snapshot(
    mqtt_data: MqttSnapshot,
    devices: list[str],
    interface: str,
) -> dict[str, Any]:
    ap = get_ap_info(interface)
    stations = get_station_info(interface)

    result: dict[str, Any] = {
        "ap": ap,
        "devices": {},
    }

    for device in devices:
        info = mqtt_data.info.get(device, {})
        status = mqtt_data.status.get(device, {})
        heartbeat = mqtt_data.heartbeat.get(device, {})

        network = info.get("network", {})
        if not isinstance(network, dict):
            network = {}

        battery_info = info.get("battery", {})
        if not isinstance(battery_info, dict):
            battery_info = {}

        battery_hb = heartbeat.get("battery", {})
        if not isinstance(battery_hb, dict):
            battery_hb = {}

        battery = battery_hb or battery_info

        mac = str(network.get("mac", "")).lower() or None
        station = stations.get(mac, {}) if mac else {}

        ip = network.get("ip")

        channel = first_not_none(
            heartbeat.get("channel"),
            status.get("channel"),
            ap.get("channel"),
        )

        rssi = first_not_none(
            heartbeat.get("rssi"),
            status.get("rssi"),
            station.get("signal_dbm"),
        )

        firmware = first_not_none(
            info.get("firmware_version"),
            heartbeat.get("firmware_version"),
            status.get("firmware_version"),
        )

        slot = first_not_none(
            info.get("ota_slot"),
            heartbeat.get("ota_slot"),
            status.get("ota_slot"),
        )

        state = first_not_none(
            heartbeat.get("state"),
            status.get("state"),
            "INFO" if info else None,
        )

        ping = ping_ms(ip)

        battery_valid = battery.get("valid")
        battery_mv = battery.get("voltage_mv")
        battery_pct = battery.get("percent_estimate")

        result["devices"][device] = {
            "fresh_heartbeat": device in mqtt_data.heartbeat,
            "state": state,
            "firmware_version": firmware,
            "ota_slot": slot,
            "idf_version": info.get("idf_version"),
            "ip": ip,
            "gateway": network.get("gateway"),
            "mac": mac,
            "battery": {
                "valid": battery_valid,
                "voltage_mv": battery_mv,
                "percent_estimate": battery_pct,
            },
            "wifi_connected": first_not_none(
                heartbeat.get("wifi_connected"),
                status.get("wifi_connected"),
            ),
            "mqtt_connected": first_not_none(
                heartbeat.get("mqtt_connected"),
                status.get("mqtt_connected"),
            ),
            "rssi_dbm": rssi,
            "channel": channel,
            "band": band_from_channel(channel),
            "uptime_ms": first_not_none(
                heartbeat.get("uptime_ms"),
                status.get("uptime_ms"),
            ),
            "ping_ms": ping,
            "ap_station": station,
        }

    return result


def battery_text(device: dict[str, Any]) -> tuple[str, str]:
    bat = device.get("battery", {})
    if not isinstance(bat, dict) or bat.get("valid") is False:
        return "-", "-"

    mv = bat.get("voltage_mv")
    pct = bat.get("percent_estimate")

    voltage = "-"
    percentage = "-"

    try:
        voltage = f"{int(mv) / 1000:.3f} V"
    except (TypeError, ValueError):
        pass

    try:
        percentage = f"{int(pct)} %"
    except (TypeError, ValueError):
        pass

    return voltage, percentage


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    def format_row(row: list[str]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(row))

    separator = "  ".join("-" * width for width in widths)
    return "\n".join(
        [
            format_row(headers),
            separator,
            *(format_row(row) for row in rows),
        ]
    )


def print_human(snapshot: dict[str, Any]) -> None:
    ap = snapshot["ap"]

    print()
    print("============================================================")
    print(" WIFI SENSING - ESTADO GENERAL RX")
    print("============================================================")

    ap_bits = []
    if ap.get("interface"):
        ap_bits.append(str(ap["interface"]))
    if ap.get("ssid"):
        ap_bits.append(str(ap["ssid"]))
    if ap.get("channel") is not None:
        ap_bits.append(
            f"{band_from_channel(ap['channel'])} ch{ap['channel']}"
        )
    if ap.get("width_mhz") is not None:
        ap_bits.append(f"{ap['width_mhz']} MHz")
    if ap.get("txpower_dbm") is not None:
        ap_bits.append(f"TX {ap['txpower_dbm']:.1f} dBm")

    print("AP:", " | ".join(ap_bits) if ap_bits else "sin datos de iw")
    print()

    rows: list[list[str]] = []

    for name, dev in snapshot["devices"].items():
        voltage, percentage = battery_text(dev)

        state = str(dev.get("state") or "-")
        if dev.get("fresh_heartbeat"):
            state = "OK/HB"

        rssi = dev.get("rssi_dbm")
        rssi_text = f"{rssi} dBm" if rssi is not None else "-"

        channel = dev.get("channel")
        band = dev.get("band") or "-"
        channel_text = f"{band}/{channel}" if channel is not None else "-"

        ping = dev.get("ping_ms")
        ping_text = f"{ping:.2f} ms" if ping is not None else "sin respuesta"

        fw = dev.get("firmware_version") or "-"
        slot = dev.get("ota_slot") or "-"
        fw_text = f"{fw}/{slot}"

        rows.append(
            [
                name,
                state,
                percentage,
                voltage,
                fw_text,
                str(dev.get("ip") or "-"),
                rssi_text,
                channel_text,
                ping_text,
                format_uptime(dev.get("uptime_ms")),
            ]
        )

    print(
        table(
            rows,
            [
                "RX",
                "Estado",
                "Bat.",
                "Voltaje",
                "FW/slot",
                "IP",
                "RSSI",
                "Banda/ch",
                "Ping",
                "Uptime",
            ],
        )
    )

    print()
    print("Detalle de enlace:")

    for name, dev in snapshot["devices"].items():
        station = dev.get("ap_station", {})
        if not isinstance(station, dict):
            station = {}

        mac = dev.get("mac") or "-"
        gateway = dev.get("gateway") or "-"
        idf = dev.get("idf_version") or "-"

        link_parts = [
            f"{name}: MAC={mac}",
            f"GW={gateway}",
            f"IDF={idf}",
        ]

        if station.get("tx_mbps") is not None:
            link_parts.append(f"TX={station['tx_mbps']:.1f} Mbit/s")
        if station.get("rx_mbps") is not None:
            link_parts.append(f"RX={station['rx_mbps']:.1f} Mbit/s")
        if station.get("inactive_ms") is not None:
            link_parts.append(f"inactive={station['inactive_ms']} ms")

        print("  " + " | ".join(link_parts))

    warnings: list[str] = []
    for name, dev in snapshot["devices"].items():
        if not dev.get("fresh_heartbeat"):
            warnings.append(
                f"{name}: no se recibió HEARTBEAT fresco durante la consulta"
            )

        bat = dev.get("battery", {})
        if isinstance(bat, dict):
            pct = bat.get("percent_estimate")
            try:
                if pct is not None and int(pct) <= 20:
                    warnings.append(
                        f"{name}: batería baja ({int(pct)} %)"
                    )
            except (TypeError, ValueError):
                pass

        if dev.get("ping_ms") is None and dev.get("ip"):
            warnings.append(
                f"{name}: MQTT tiene INFO pero el ping no respondió"
            )

    if warnings:
        print()
        print("Avisos:")
        for warning in warnings:
            print(f"  - {warning}")

    print()
    print(
        "Nota: el porcentaje de batería es una estimación; "
        "voltage_mv es la medición principal."
    )
    print("============================================================")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consulta estado general de los receptores WiFi Sensing."
    )
    parser.add_argument(
        "--devices",
        default=None,
        help="Dispositivos separados por coma. Por defecto usa RX_DEVICES.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=12.0,
        help=(
            "Segundos máximos esperando HEARTBEAT fresco. "
            "El firmware usa 10 s por defecto."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Entrega el resultado como JSON.",
    )
    args = parser.parse_args()

    if args.timeout <= 0:
        raise SystemExit("--timeout debe ser mayor que 0")

    env = dotenv_values(ENV_FILE)

    host = env_required(env, "MQTT_HOST")
    port = int(str(env.get("MQTT_PORT", "1883")).strip() or "1883")
    user = env_required(env, "MQTT_SERVER_USER")
    password = env_required(env, "MQTT_SERVER_PASSWORD")
    base_topic = str(
        env.get("MQTT_BASE_TOPIC", "wifi-sensing/v1")
    ).strip() or "wifi-sensing/v1"

    interface = str(
        env.get("WIFI_AP_INTERFACE", "wlo1")
    ).strip() or "wlo1"

    devices = parse_devices(args.devices, env)

    collector = MqttSnapshot(
        host=host,
        port=port,
        user=user,
        password=password,
        base_topic=base_topic,
        devices=devices,
    )

    try:
        collector.collect(timeout=args.timeout)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    snapshot = build_snapshot(
        mqtt_data=collector,
        devices=devices,
        interface=interface,
    )

    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    else:
        print_human(snapshot)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
