#!/usr/bin/env python3

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


if not ENV_FILE.exists():
    raise SystemExit(
        f"No existe {ENV_FILE}"
    )


env = dotenv_values(ENV_FILE)


def required(name):
    value = env.get(name)

    if value is None or str(value).strip() == "":
        raise SystemExit(
            f"Variable obligatoria no definida: {name}"
        )

    return str(value).strip()


MQTT_HOST = required("MQTT_HOST")
MQTT_PORT = int(env.get("MQTT_PORT", 1883))

MQTT_USER = required("MQTT_SERVER_USER")
MQTT_PASSWORD = required("MQTT_SERVER_PASSWORD")

BASE_TOPIC = env.get(
    "MQTT_BASE_TOPIC",
    "wifi-sensing/v1",
).strip()

OTA_HOST = required("OTA_HOST")
OTA_PORT = int(env.get("OTA_PORT", 8000))

OTA_DIR_RAW = env.get(
    "OTA_DIR",
    "./ota-server",
).strip()


OTA_DIR = Path(OTA_DIR_RAW)

if not OTA_DIR.is_absolute():
    OTA_DIR = ROOT / OTA_DIR


RX_DEVICES = {
    x.strip()
    for x in env.get(
        "RX_DEVICES",
        "",
    ).split(",")
    if x.strip()
}


class OtaUpdater:

    def __init__(
        self,
        device,
        version,
        filename,
        timeout,
    ):
        self.device = device
        self.version = version
        self.filename = filename
        self.timeout = timeout

        self.connected = threading.Event()
        self.finished = threading.Event()

        self.success = False
        self.failure_reason = None

        self.seen_starting = False
        self.seen_downloading = False
        self.seen_success = False
        self.seen_online = False
        self.seen_ready = False

        self.status_topic = (
            f"{BASE_TOPIC}/devices/"
            f"{device}/status"
        )

        self.command_topic = (
            f"{BASE_TOPIC}/devices/"
            f"{device}/cmd/ota"
        )

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=(
                f"ota-updater-{device}-"
                f"{int(time.time())}"
            ),
            protocol=mqtt.MQTTv311,
        )

        self.client.username_pw_set(
            MQTT_USER,
            MQTT_PASSWORD,
        )

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect


    def on_connect(
        self,
        client,
        userdata,
        flags,
        reason_code,
        properties,
    ):
        if reason_code != 0:
            self.failure_reason = (
                f"MQTT connection rejected: "
                f"{reason_code}"
            )

            self.finished.set()
            return

        client.subscribe(
            self.status_topic,
            qos=1,
        )

        self.connected.set()


    def on_disconnect(
        self,
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties,
    ):
        pass


    def on_message(
        self,
        client,
        userdata,
        msg,
    ):
        try:
            payload = json.loads(
                msg.payload.decode("utf-8")
            )

        except Exception:
            return


        state = payload.get("state")

        firmware_version = payload.get(
            "firmware_version"
        )

        slot = payload.get(
            "ota_slot"
        )

        detail = payload.get(
            "detail"
        )


        print(
            f"[{self.device}] "
            f"{state or '?'}",
            end="",
        )


        if firmware_version:
            print(
                f" version={firmware_version}",
                end="",
            )


        if slot:
            print(
                f" slot={slot}",
                end="",
            )


        if detail:
            print(
                f" detail={detail}",
                end="",
            )


        print()


        if state == "OTA_STARTING":
            self.seen_starting = True


        elif state == "OTA_DOWNLOADING":
            self.seen_downloading = True


        elif state == "OTA_SUCCESS":
            self.seen_success = True


        elif state == "OTA_FAILED":
            self.failure_reason = (
                detail
                or "OTA_FAILED"
            )

            self.finished.set()


        elif state == "OTA_REJECTED":
            self.failure_reason = (
                detail
                or "OTA_REJECTED"
            )

            self.finished.set()


        elif state == "ONLINE":

            if (
                self.seen_success
                and firmware_version == self.version
            ):
                self.seen_online = True

                self.success = True
                self.finished.set()


        elif state == "READY":

            if self.seen_success:
                self.seen_ready = True


    def verify_http(self):
        url = (
            f"http://{OTA_HOST}:"
            f"{OTA_PORT}/"
            f"{self.filename}"
        )


        print(
            f"[CHECK] HTTP: {url}"
        )


        request = urllib.request.Request(
            url,
            method="HEAD",
        )


        try:
            with urllib.request.urlopen(
                request,
                timeout=5,
            ) as response:

                if response.status != 200:
                    raise RuntimeError(
                        f"HTTP {response.status}"
                    )


                length = response.headers.get(
                    "Content-Length"
                )


                print(
                    "[OK] Firmware disponible",
                    (
                        f"({length} bytes)"
                        if length
                        else ""
                    ),
                )


        except Exception as exc:
            raise RuntimeError(
                "Firmware no disponible "
                f"por HTTP: {exc}"
            ) from exc


    def run(self):

        firmware_path = (
            OTA_DIR
            / self.filename
        )


        if not firmware_path.exists():
            raise RuntimeError(
                "No existe firmware local: "
                f"{firmware_path}"
            )


        print()
        print(
            "======================================"
        )
        print(
            " WIFI SENSING - OTA"
        )
        print(
            "======================================"
        )
        print(
            f"device   : {self.device}"
        )
        print(
            f"version  : {self.version}"
        )
        print(
            f"firmware : {firmware_path}"
        )
        print(
            f"broker   : "
            f"{MQTT_HOST}:{MQTT_PORT}"
        )
        print(
            "======================================"
        )
        print()


        self.verify_http()


        self.client.connect(
            MQTT_HOST,
            MQTT_PORT,
            30,
        )


        self.client.loop_start()


        try:

            if not self.connected.wait(10):
                raise RuntimeError(
                    "Timeout conectando MQTT"
                )


            command = {
                "version":
                    self.version,

                "filename":
                    self.filename,
            }


            print(
                f"[MQTT] Publicando "
                f"{self.command_topic}"
            )


            info = self.client.publish(
                self.command_topic,
                json.dumps(
                    command,
                    separators=(",", ":"),
                ),
                qos=1,
                retain=False,
            )


            info.wait_for_publish()


            deadline = (
                time.monotonic()
                + self.timeout
            )


            while (
                not self.finished.is_set()
            ):
                remaining = (
                    deadline
                    - time.monotonic()
                )


                if remaining <= 0:
                    raise TimeoutError(
                        "Timeout esperando "
                        "finalización OTA"
                    )


                self.finished.wait(
                    min(
                        remaining,
                        1.0,
                    )
                )


            if not self.success:
                raise RuntimeError(
                    "OTA falló: "
                    f"{self.failure_reason}"
                )


            print()
            print(
                "======================================"
            )
            print(
                " OTA COMPLETADA"
            )
            print(
                "======================================"
            )

            print(
                f"device  : {self.device}"
            )

            print(
                f"version : {self.version}"
            )

            print(
                "ONLINE  : OK"
            )

            print(
                "READY   : "
                + (
                    "OK"
                    if self.seen_ready
                    else "no requerido"
                )
            )


            if self.seen_success:
                print(
                    "OTA_SUCCESS : OK"
                )


            print(
                "======================================"
            )


        finally:

            self.client.disconnect()
            self.client.loop_stop()


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Actualiza un receptor "
            "WiFi Sensing mediante OTA"
        )
    )


    parser.add_argument(
        "device",
        help="rx01, rx02 o rx03",
    )


    parser.add_argument(
        "version",
        help="versión destino, ej. 1.0.2",
    )


    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="timeout total OTA en segundos",
    )


    args = parser.parse_args()


    if args.device not in RX_DEVICES:
        raise SystemExit(
            "Dispositivo no configurado: "
            f"{args.device}"
        )


    filename = (
        f"wifi_sensing_"
        f"{args.device}_"
        f"{args.version}.bin"
    )


    updater = OtaUpdater(
        device=args.device,
        version=args.version,
        filename=filename,
        timeout=args.timeout,
    )


    try:
        updater.run()

    except Exception as exc:
        print(
            f"\n[ERROR] {exc}",
            file=sys.stderr,
        )

        return 1


    return 0


if __name__ == "__main__":
    raise SystemExit(main())
