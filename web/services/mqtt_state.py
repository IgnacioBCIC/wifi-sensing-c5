from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class MqttState:
    def __init__(self):
        env = dotenv_values(ENV_FILE)

        self.host = str(env.get("MQTT_HOST", "127.0.0.1"))
        self.port = int(env.get("MQTT_PORT", 1883))
        self.user = str(env.get("MQTT_SERVER_USER", ""))
        self.password = str(env.get("MQTT_SERVER_PASSWORD", ""))
        self.base = str(env.get("MQTT_BASE_TOPIC", "wifi-sensing/v1")).rstrip("/")
        raw_devices = str(env.get("RX_DEVICES", "rx01,rx02,rx03"))
        self.devices = [x.strip() for x in raw_devices.split(",") if x.strip()]

        self.lock = threading.RLock()
        self.connected = False
        self.info: dict[str, dict[str, Any]] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self.ready: dict[str, dict[str, Any]] = {}
        self.last_seen: dict[str, float] = {}
        self.health_events = deque(maxlen=500)
        self.model_events = deque(maxlen=500)

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"wifi-sensing-web-{int(time.time())}",
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(self.user, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self):
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = bool(
            reason_code.is_failure
            if hasattr(reason_code, "is_failure")
            else int(getattr(reason_code, "value", reason_code)) != 0
        )
        if failed:
            return

        with self.lock:
            self.connected = True

        client.subscribe(
            [
                (f"{self.base}/devices/+/info", 1),
                (f"{self.base}/devices/+/status", 1),
                (f"{self.base}/control/csi-health", 1),
                (f"{self.base}/control/espectre-result", 1),
                (f"{self.base}/control/ae-result", 1),
            ]
        )

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties=None
    ):
        with self.lock:
            self.connected = False

    def _on_message(self, client, userdata, msg):
        now = time.time()
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        topic = msg.topic

        with self.lock:
            if topic == f"{self.base}/control/csi-health":
                event = dict(payload)
                event["_received_at"] = now
                self.health_events.append(event)
                return

            if topic == f"{self.base}/control/espectre-result":
                event = dict(payload)
                event["_received_at"] = now
                event["_model_family"] = "zero_shot_a"
                self.model_events.append(event)
                return

            if topic == f"{self.base}/control/ae-result":
                event = dict(payload)
                event["_received_at"] = now
                event["_model_family"] = "ae_hybrid_v1"
                self.model_events.append(event)
                return

            prefix = f"{self.base}/devices/"
            if not topic.startswith(prefix):
                return

            rest = topic[len(prefix):]
            parts = rest.split("/")
            if len(parts) < 2:
                return
            device, kind = parts[0], parts[1]

            if kind == "info":
                self.info[device] = payload
            elif kind == "status":
                self.status[device] = payload
                self.last_seen[device] = now

                if payload.get("state") == "READY":
                    self.ready[device] = payload

    @staticmethod
    def _pick(*values):
        for value in values:
            if value is not None:
                return value
        return None

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            devices = {}
            for device in self.devices:
                info = dict(self.info.get(device, {}))
                status = dict(self.status.get(device, {}))
                ready = dict(self.ready.get(device, {}))

                network = (
                    info.get("network")
                    if isinstance(info.get("network"), dict)
                    else {}
                )

                ready_network = (
                    ready.get("network")
                    if isinstance(ready.get("network"), dict)
                    else {}
                )

                applied = (
                    ready.get("applied")
                    if isinstance(ready.get("applied"), dict)
                    else {}
                )

                battery = status.get("battery")
                if not isinstance(battery, dict):
                    battery = info.get("battery")
                if not isinstance(battery, dict):
                    battery = {}

                seen = self.last_seen.get(device)
                age = (now - seen) if seen else None
                online = age is not None and age <= 25.0

                channel = self._pick(
                    status.get("channel"),
                    applied.get("channel"),
                    info.get("channel"),
                )

                try:
                    channel_int = int(channel) if channel is not None else None
                except (TypeError, ValueError):
                    channel_int = None

                band = self._pick(
                    status.get("band"),
                    applied.get("band"),
                )

                if band is None and channel_int is not None:
                    band = "2.4g" if channel_int <= 14 else "5g"

                latest_health = None
                for event in reversed(self.health_events):
                    if event.get("device_id") == device:
                        latest_health = dict(event)
                        break

                devices[device] = {
                    "device_id": device,
                    "online": online,
                    "last_seen_age_s": round(age, 1) if age is not None else None,
                    "state": status.get("state"),
                    "firmware_version": self._pick(
                        info.get("firmware_version"), status.get("firmware_version")
                    ),
                    "ota_slot": self._pick(info.get("ota_slot"), status.get("ota_slot")),
                    "idf_version": info.get("idf_version"),
                    "ip": network.get("ip"),
                    "gateway": network.get("gateway"),
                    "mac": network.get("mac"),
                    "battery": battery,
                    "rssi": self._pick(
                        status.get("rssi"),
                        ready_network.get("rssi"),
                    ),
                    "channel": channel_int,
                    "band": band,
                    "wifi_connected": status.get("wifi_connected"),
                    "mqtt_connected": status.get("mqtt_connected"),
                    "uptime_ms": status.get("uptime_ms"),
                    "health": latest_health,
                }

            return {
                "mqtt_connected": self.connected,
                "mqtt_host": self.host,
                "devices": devices,
                "timestamp": now,
            }

    def events_since(self, started_at: float) -> dict[str, list[dict[str, Any]]]:
        with self.lock:
            health = [
                dict(x) for x in self.health_events
                if float(x.get("_received_at", 0)) >= started_at
            ]
            model = [
                dict(x) for x in self.model_events
                if float(x.get("_received_at", 0)) >= started_at
            ]
        return {"health": health, "model": model}
