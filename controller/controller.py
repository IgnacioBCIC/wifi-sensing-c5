#!/usr/bin/env python3

import argparse
import copy
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from settings import (
    MQTT_HOST,
    MQTT_PORT,
    MQTT_SERVER_USER,
    MQTT_SERVER_PASSWORD,
    MQTT_BASE_TOPIC,
    RX_DEVICES,
    RX_BASE_MAC,
    SERVER_NAME,
    WIFI_AP_INTERFACE,
    WIFI_AP_SSID,
    WIFI_AP_IPV4_CIDR,
    WIFI_AP_PROFILE_24,
    WIFI_AP_CHANNEL_24,
    WIFI_AP_PROFILE_5,
    WIFI_AP_CHANNEL_5,
    WIFI_AP_BANDWIDTH_MHZ,
    WIFI_AP_VERIFY_TIMEOUT_S,
    WIFI_AP_ACTIVATION_RETRIES,
    WIFI_AP_RETRY_DELAY_S,
    UDP_TARGET_PORT,
    UDP_BROADCAST,
    RECOVERY_UDP_ENABLED,
    RECOVERY_UDP_PORT,
    RECOVERY_UDP_TIMEOUT_MS,
    RECOVERY_UDP_RETRIES,
    get_rx_credentials,
)

from probe_generator import ProbeGenerator


BASE_TOPIC = MQTT_BASE_TOPIC


class ExperimentController:
    def __init__(self, cfg: dict, devices=None, health_only: bool = False, live_mode: bool = False, live_stop_file: str | None = None):
        self.cfg = cfg

        self.health_only = bool(health_only)

        self.live_mode = bool(live_mode)
        self.live_stop_file = (
            Path(live_stop_file) if live_stop_file else None
        )

        self.broker_host = MQTT_HOST
        self.broker_port = MQTT_PORT
        self.username = MQTT_SERVER_USER
        self.password = MQTT_SERVER_PASSWORD

        self.devices = set(devices if devices is not None else RX_DEVICES)
        if not self.devices:
            raise RuntimeError("No hay receptores seleccionados")


        self._last_config_id = int(time.time() * 1000)
        self.config_id = self._last_config_id

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.experiment_id = f"exp_{timestamp}"
        self.session_id = f"{self.experiment_id}_session_001"


        self.connected = threading.Event()
        self.condition = threading.Condition()

        self.ready_devices = set()
        self.armed_devices = set()
        self.csi_complete_devices = set()

        self.online_devices = set()
        self.restart_ack_devices = set()
        self.restart_rejected_devices = set()


        self.csi_reinit_ack_ids = {}
        self.csi_reinit_rejected = {}

        self.current_block_id = 0
        self.ready_expected_devices = set(self.devices)
        self.capture_expected_devices = set(self.devices)

        self.csi_complete_payloads = {}
        self.health_results = {}
        self.block_decision_acks = {}


        self.device_info = {}


        self.last_mqtt_seen = {}
        self.heartbeat_payloads = {}

        self.probe_generator = None
        self.udp_targets = {}


        recovery = self.cfg.get("recovery", {})

        self.association_timeout_s = float(
            recovery.get("association_timeout_s", 25.0)
        )
        self.info_timeout_s = float(
            recovery.get("info_timeout_s", 25.0)
        )
        self.ready_retry_timeout_s = float(
            recovery.get("ready_retry_timeout_s", 8.0)
        )
        self.restart_enabled = bool(
            recovery.get("restart_enabled", True)
        )
        self.restart_command_retries = int(
            recovery.get("restart_command_retries", 3)
        )
        self.restart_ack_timeout_s = float(
            recovery.get("restart_ack_timeout_s", 5.0)
        )
        self.restart_online_timeout_s = float(
            recovery.get("restart_online_timeout_s", 25.0)
        )
        self.restart_ready_timeout_s = float(
            recovery.get("restart_ready_timeout_s", 20.0)
        )

        if self.restart_command_retries < 1:
            raise RuntimeError(
                "recovery.restart_command_retries debe ser >= 1"
            )


        health = self.cfg.get("healthcheck", {})

        self.health_enabled = bool(health.get("enabled", True))
        self.health_rate_hz = float(
            health.get(
                "rate_hz",
                self.cfg.get("probe", {}).get("rate_hz", 100),
            )
        )


        self.health_search_target_packets = int(
            health.get("search_target_packets", 24)
        )
        self.health_min_frames = int(
            health.get("min_frames", 20)
        )
        self.health_search_settle_time_ms = int(
            health.get("search_settle_time_ms", 0)
        )
        self.health_max_capture_ms = int(
            health.get("max_capture_ms", 700)
        )
        self.health_result_timeout_s = float(
            health.get("result_timeout_s", 4.0)
        )


        self.health_good_unique_ratio = float(
            health.get("good_unique_ratio", 0.95)
        )
        self.health_required_consecutive = int(
            health.get("required_consecutive_passes", 3)
        )


        self.health_max_soft_reinits = int(
            health.get("max_soft_reinits_per_boot", 20)
        )
        self.health_max_full_restarts = int(
            health.get("max_full_restarts", 4)
        )
        self.health_reinit_ack_timeout_s = float(
            health.get("reinit_ack_timeout_s", 2.0)
        )
        self.health_reinit_pause_ms = int(
            health.get("reinit_pause_ms", 20)
        )
        self.health_between_checks_ms = int(
            health.get("between_checks_ms", 30)
        )

        if self.health_min_frames < 20:
            raise RuntimeError(
                "healthcheck.min_frames debe ser >= 20 "
                "con el csi_receiver actual"
            )
        if self.health_search_target_packets < self.health_min_frames:
            raise RuntimeError(
                "healthcheck.search_target_packets debe ser >= "
                "healthcheck.min_frames"
            )
        if self.health_rate_hz <= 0:
            raise RuntimeError("healthcheck.rate_hz debe ser > 0")
        if not (0.0 < self.health_good_unique_ratio <= 1.0):
            raise RuntimeError(
                "healthcheck.good_unique_ratio debe estar en (0,1]"
            )
        if self.health_required_consecutive < 1:
            raise RuntimeError(
                "healthcheck.required_consecutive_passes debe ser >= 1"
            )
        if self.health_max_soft_reinits < 1:
            raise RuntimeError(
                "healthcheck.max_soft_reinits_per_boot debe ser >= 1"
            )
        if self.health_max_full_restarts < 0:
            raise RuntimeError(
                "healthcheck.max_full_restarts debe ser >= 0"
            )


        block_quality = self.cfg.get("block_quality", {})
        self.block_max_attempts = int(
            block_quality.get("max_attempts", 3)
        )
        self.block_decision_timeout_s = float(
            block_quality.get("decision_timeout_s", 5.0)
        )
        self.block_retry_settle_time_ms = int(
            block_quality.get("retry_settle_time_ms", 500)
        )

        if self.block_max_attempts < 1:
            raise RuntimeError(
                "block_quality.max_attempts debe ser >= 1"
            )
        if self.block_decision_timeout_s <= 0:
            raise RuntimeError(
                "block_quality.decision_timeout_s debe ser > 0"
            )
        if self.block_retry_settle_time_ms < 0:
            raise RuntimeError(
                "block_quality.retry_settle_time_ms debe ser >= 0"
            )


        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=SERVER_NAME,
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(
            self.username,
            self.password,
        )
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message


    def _new_config_id(self):
        now = int(time.time() * 1000)
        self._last_config_id = max(self._last_config_id + 1, now)
        return self._last_config_id

    @staticmethod
    def _run_command(args):
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"código {result.returncode}"
            )
            raise RuntimeError(
                f"Falló comando: {' '.join(args)}\n{detail}"
            )
        return result.stdout.strip()


    def get_associated_stations(self):
        output = self._run_command(
            ["iw", "dev", WIFI_AP_INTERFACE, "station", "dump"]
        )

        stations = {}
        pattern = re.compile(
            r"^Station\s+([0-9a-fA-F:]{17})\s+\(on\s+[^)]+\)\n"
            r"(.*?)(?=^Station\s+|\Z)",
            re.MULTILINE | re.DOTALL,
        )

        for match in pattern.finditer(output):
            mac = match.group(1).lower()
            body = match.group(2)
            signal_match = re.search(
                r"^\s*signal:\s*(-?\d+)",
                body,
                re.MULTILINE,
            )
            stations[mac] = {
                "mac": mac,
                "signal_dbm": (
                    int(signal_match.group(1))
                    if signal_match
                    else None
                ),
            }

        return stations

    def get_associated_device_status(self, devices=None):
        expected_devices = set(
            devices if devices is not None else self.devices
        )
        stations = self.get_associated_stations()
        result = {}

        for device_id in sorted(expected_devices):
            expected_mac = (
                RX_BASE_MAC.get(device_id, "").strip().lower()
            )
            station = stations.get(expected_mac)

            result[device_id] = {
                "device_id": device_id,
                "mac": expected_mac,
                "associated": station is not None,
                "signal_dbm": (
                    station.get("signal_dbm")
                    if station
                    else None
                ),
            }

        return result

    def wait_for_associated_devices(self, devices=None, timeout=None):
        expected_devices = set(
            devices if devices is not None else self.devices
        )
        timeout = float(
            self.association_timeout_s
            if timeout is None
            else timeout
        )

        deadline = time.monotonic() + timeout
        last_missing = None

        while time.monotonic() < deadline:
            status = self.get_associated_device_status(
                expected_devices
            )
            missing = {
                device_id
                for device_id, item in status.items()
                if not item["associated"]
            }

            if not missing:
                return status

            if missing != last_missing:
                print(
                    "[AP] Esperando asociación: "
                    + ", ".join(sorted(missing))
                )
                last_missing = set(missing)

            time.sleep(0.5)

        status = self.get_associated_device_status(
            expected_devices
        )
        missing = {
            device_id
            for device_id, item in status.items()
            if not item["associated"]
        }

        raise TimeoutError(
            "No se asociaron al hotspot: "
            + ", ".join(sorted(missing))
        )

    def _resolve_hotspot_target(self):
        sensing = self.cfg.get("sensing", {})
        band = sensing.get("band")

        if band == "2.4g":
            profile = WIFI_AP_PROFILE_24
            channel = WIFI_AP_CHANNEL_24
        elif band == "5g":
            profile = WIFI_AP_PROFILE_5
            channel = WIFI_AP_CHANNEL_5
        else:
            raise RuntimeError(
                "sensing.band debe ser '2.4g' o '5g'"
            )

        if sensing.get("phy") != "802.11n":
            raise RuntimeError(
                "sólo se admite sensing.phy=802.11n"
            )

        configured_bw = int(
            sensing.get("bandwidth_mhz", 0)
        )
        if configured_bw != WIFI_AP_BANDWIDTH_MHZ:
            raise RuntimeError(
                "El ancho solicitado por experiment.json "
                f"({configured_bw} MHz) no coincide con el AP "
                f"({WIFI_AP_BANDWIDTH_MHZ} MHz)"
            )

        requested_channel = int(
            sensing.get("expected_channel", 0)
        )
        if requested_channel not in (0, channel):
            raise RuntimeError(
                "El canal solicitado por experiment.json "
                f"({requested_channel}) no coincide con "
                f"{profile} (canal {channel})"
            )


        sensing["expected_channel"] = channel

        return profile, channel

    def _read_hotspot_state(self):
        iw_output = self._run_command(
            ["iw", "dev", WIFI_AP_INTERFACE, "info"]
        )
        connection = self._run_command(
            [
                "nmcli",
                "-g",
                "GENERAL.CONNECTION",
                "device",
                "show",
                WIFI_AP_INTERFACE,
            ]
        )
        ip_output = self._run_command(
            [
                "ip",
                "-4",
                "-o",
                "addr",
                "show",
                "dev",
                WIFI_AP_INTERFACE,
            ]
        )

        ssid_match = re.search(
            r"^\s*ssid\s+(.+)$",
            iw_output,
            re.MULTILINE,
        )
        type_match = re.search(
            r"^\s*type\s+(\S+)$",
            iw_output,
            re.MULTILINE,
        )
        channel_match = re.search(
            r"^\s*channel\s+(\d+).*?width:\s*(\d+)\s*MHz",
            iw_output,
            re.MULTILINE,
        )
        ip_match = re.search(
            r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)",
            ip_output,
        )

        return {
            "connection": connection,
            "ssid": (
                ssid_match.group(1).strip()
                if ssid_match
                else None
            ),
            "type": (
                type_match.group(1).strip()
                if type_match
                else None
            ),
            "channel": (
                int(channel_match.group(1))
                if channel_match
                else None
            ),
            "bandwidth_mhz": (
                int(channel_match.group(2))
                if channel_match
                else None
            ),
            "ipv4_cidr": (
                ip_match.group(1)
                if ip_match
                else None
            ),
        }

    @staticmethod
    def _hotspot_matches(state, profile, channel):
        if not state:
            return False

        return (
            state.get("connection") == profile
            and state.get("ssid") == WIFI_AP_SSID
            and state.get("type") == "AP"
            and state.get("channel") == channel
            and state.get("bandwidth_mhz")
            == WIFI_AP_BANDWIDTH_MHZ
            and state.get("ipv4_cidr")
            == WIFI_AP_IPV4_CIDR
        )

    def _wait_hotspot_state(
        self,
        profile,
        channel,
        timeout_s,
    ):
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        last_state = None

        while time.monotonic() < deadline:
            try:
                state = self._read_hotspot_state()
                last_state = state

                if self._hotspot_matches(
                    state,
                    profile,
                    channel,
                ):
                    return True, state

            except RuntimeError as exc:
                last_state = {"error": str(exc)}

            time.sleep(0.5)

        return False, last_state

    @staticmethod
    def _nmcli_best_effort(args):
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
        )

    def _reset_hotspot_interface(self, target_profile):
        print(
            "[AP] Limpiando estado transitorio de NetworkManager "
            "antes del reintento..."
        )


        for candidate in dict.fromkeys(
            [
                target_profile,
                WIFI_AP_PROFILE_24,
                WIFI_AP_PROFILE_5,
            ]
        ):
            result = self._nmcli_best_effort(
                ["nmcli", "connection", "down", candidate]
            )

            if result.returncode == 0:
                print(f"[AP] Perfil desactivado: {candidate}")

        if WIFI_AP_RETRY_DELAY_S > 0:
            print(
                f"[AP] Esperando {WIFI_AP_RETRY_DELAY_S} s "
                "antes de reintentar..."
            )
            time.sleep(WIFI_AP_RETRY_DELAY_S)

    def _activate_hotspot_with_retries(
        self,
        profile,
        channel,
    ):
        attempts = int(WIFI_AP_ACTIVATION_RETRIES)

        if attempts < 1:
            raise RuntimeError(
                "WIFI_AP_ACTIVATION_RETRIES debe ser >= 1"
            )

        last_state = None
        last_error = None

        for attempt in range(1, attempts + 1):
            print(
                f"[AP] Activación intento {attempt}/{attempts}: "
                f"{profile}"
            )

            result = self._nmcli_best_effort(
                [
                    "nmcli",
                    "connection",
                    "up",
                    profile,
                    "ifname",
                    WIFI_AP_INTERFACE,
                ]
            )

            if result.returncode == 0:
                last_error = None
            else:
                last_error = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"código {result.returncode}"
                )
                print(
                    f"[WARN] nmcli no pudo activar {profile}: "
                    f"{last_error}"
                )


            verify_timeout = (
                WIFI_AP_VERIFY_TIMEOUT_S
                if result.returncode == 0
                else min(3, WIFI_AP_VERIFY_TIMEOUT_S)
            )

            matched, state = self._wait_hotspot_state(
                profile,
                channel,
                verify_timeout,
            )
            last_state = state

            if matched:
                return state

            if attempt < attempts:
                print(
                    "[WARN] El AP todavía no alcanzó el estado "
                    "esperado; se realizará recuperación de "
                    "NetworkManager."
                )
                self._reset_hotspot_interface(profile)

        detail = (
            f"Último error nmcli: {last_error}. "
            if last_error
            else ""
        )

        raise RuntimeError(
            "No fue posible activar el hotspot después de "
            f"{attempts} intento(s). "
            f"{detail}Último estado: {last_state}"
        )

    def configure_hotspot(self):
        profile, channel = self._resolve_hotspot_target()
        band = self.cfg["sensing"]["band"]

        print()
        print("======================================")
        print(" CONFIGURACION DEL HOTSPOT")
        print("======================================")
        print(f"[AP] Banda solicitada : {band}")
        print(f"[AP] Perfil            : {profile}")
        print(f"[AP] Interfaz          : {WIFI_AP_INTERFACE}")
        print(f"[AP] Canal esperado    : {channel}")
        print(f"[AP] BW esperado       : {WIFI_AP_BANDWIDTH_MHZ} MHz")

        try:
            current_state = self._read_hotspot_state()
        except RuntimeError:
            current_state = None

        if self._hotspot_matches(
            current_state,
            profile,
            channel,
        ):
            print()
            print("[AP] Hotspot ya activo y correcto")
            print("[AP] No se reactiva NetworkManager")
            state = current_state

        else:
            print()
            print("[AP] Activando perfil del hotspot...")


            with self.condition:
                self.device_info.clear()
                self.online_devices.clear()
                self.ready_devices.clear()

            state = self._activate_hotspot_with_retries(
                profile,
                channel,
            )


        if not self._hotspot_matches(
            state,
            profile,
            channel,
        ):
            matched, final_state = self._wait_hotspot_state(
                profile,
                channel,
                WIFI_AP_VERIFY_TIMEOUT_S,
            )

            if not matched:
                raise RuntimeError(
                    "El hotspot no alcanzó el estado esperado. "
                    f"Último estado: {final_state}"
                )

            state = final_state

        print()
        print("[AP] Hotspot validado correctamente")
        print(f"     SSID    : {state['ssid']}")
        print(f"     banda   : {band}")
        print(f"     canal   : {state['channel']}")
        print(
            f"     BW      : "
            f"{state['bandwidth_mhz']} MHz"
        )
        print(f"     IPv4    : {state['ipv4_cidr']}")


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
            f"[MQTT] Conectado a "
            f"{self.broker_host}:{self.broker_port}"
        )

        topics = [
            (f"{BASE_TOPIC}/devices/+/status", 1),
            (f"{BASE_TOPIC}/devices/+/info", 1),
            (f"{BASE_TOPIC}/control/csi-health", 1),
            (f"{BASE_TOPIC}/control/block-decision-ack", 1),
        ]

        client.subscribe(topics)

        for topic, _qos in topics:
            print(f"[MQTT] Suscrito a {topic}")

        self.connected.set()

    def on_disconnect(
        self,
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties,
    ):
        print(f"[MQTT] Desconectado: {reason_code}")
        self.connected.clear()

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(
                msg.payload.decode("utf-8")
            )
        except Exception as exc:
            print(
                f"[WARN] JSON inválido en {msg.topic}: {exc}"
            )
            return

        health_topic = (
            f"{BASE_TOPIC}/control/csi-health"
        )
        if msg.topic == health_topic:
            self.process_health_result(payload)
            return

        decision_ack_topic = (
            f"{BASE_TOPIC}/control/block-decision-ack"
        )
        if msg.topic == decision_ack_topic:
            self.process_block_decision_ack(payload)
            return

        prefix = f"{BASE_TOPIC}/devices/"
        info_suffix = "/info"
        status_suffix = "/status"

        if not msg.topic.startswith(prefix):
            return

        if msg.topic.endswith(info_suffix):
            device_id = msg.topic[
                len(prefix):-len(info_suffix)
            ]
            if device_id in self.devices:
                self.process_info(device_id, payload)
            return

        if not msg.topic.endswith(status_suffix):
            return

        device_id = msg.topic[
            len(prefix):-len(status_suffix)
        ]

        if device_id not in self.devices:
            return

        with self.condition:
            self.last_mqtt_seen[device_id] = time.monotonic()
            self.condition.notify_all()

        state = payload.get("state")

        if state == "HEARTBEAT":
            with self.condition:
                self.heartbeat_payloads[device_id] = payload
                self.condition.notify_all()
            return

        if state == "READY":
            self.process_ready(device_id, payload)

        elif state == "ARMED":
            self.process_armed(device_id, payload)

        elif state == "CSI_COMPLETE":
            self.process_csi_complete(
                device_id,
                payload,
            )

        elif state == "ONLINE":
            with self.condition:
                self.online_devices.add(device_id)
                self.condition.notify_all()

            print(
                f"[ONLINE] {device_id} "
                f"firmware={payload.get('firmware_version')} "
                f"slot={payload.get('ota_slot')}"
            )

        elif state == "RESTARTING":
            with self.condition:
                self.restart_ack_devices.add(device_id)
                self.condition.notify_all()

            print(
                f"[RECOVERY] {device_id}: "
                "RESTARTING confirmado"
            )

        elif state == "RESTART_REJECTED":
            with self.condition:
                self.restart_rejected_devices.add(
                    device_id
                )
                self.condition.notify_all()

            print(
                f"[RECOVERY] {device_id}: "
                "RESTART rechazado "
                f"({payload.get('detail')})"
            )

        elif state == "CSI_REINIT_DONE":
            request_id = payload.get("request_id")
            if isinstance(request_id, int):
                with self.condition:
                    self.csi_reinit_ack_ids[device_id] = request_id
                    self.csi_reinit_rejected.pop(device_id, None)
                    self.condition.notify_all()
            print(
                f"[CSI-RECOVERY] {device_id}: soft-reinit OK "
                f"request_id={request_id}"
            )

        elif state == "CSI_REINIT_REJECTED":
            request_id = payload.get("request_id")
            with self.condition:
                self.csi_reinit_rejected[device_id] = {
                    "request_id": request_id,
                    "detail": payload.get("detail"),
                }
                self.condition.notify_all()
            print(
                f"[CSI-RECOVERY] {device_id}: soft-reinit rechazado "
                f"({payload.get('detail')})"
            )

        else:
            print(
                f"[STATUS] {device_id}: "
                f"{state or 'UNKNOWN'}"
            )

    def process_info(self, device_id, payload):
        payload_device = payload.get("device_id")
        if payload_device != device_id:
            print(
                f"[ERROR] INFO {device_id}: "
                f"device_id payload={payload_device}"
            )
            return

        network = payload.get("network", {})

        ip = str(network.get("ip", "")).strip()
        mac = str(
            network.get("mac", "")
        ).strip().lower()
        gateway = str(
            network.get("gateway", "")
        ).strip()

        expected_mac = (
            RX_BASE_MAC.get(device_id, "")
            .strip()
            .lower()
        )

        if not expected_mac:
            print(
                f"[ERROR] {device_id}: "
                "RX_BASE_MAC no está configurada"
            )
            return

        if mac != expected_mac:
            print()
            print(
                f"[ERROR] Identidad física inválida: "
                f"{device_id}"
            )
            print(f"        esperada : {expected_mac}")
            print(f"        recibida : {mac}")
            return

        try:
            ip_obj = ipaddress.ip_address(ip)
            ap_network = ipaddress.ip_network(
                WIFI_AP_IPV4_CIDR,
                strict=False,
            )
        except ValueError as exc:
            print(
                f"[ERROR] INFO {device_id}: "
                f"IP inválida: {exc}"
            )
            return

        if ip_obj not in ap_network:
            print(
                f"[ERROR] INFO {device_id}: "
                f"{ip} fuera de {ap_network}"
            )
            return

        info = {
            "device_id": device_id,
            "firmware_version": payload.get(
                "firmware_version"
            ),
            "ota_slot": payload.get("ota_slot"),
            "ip": ip,
            "gateway": gateway,
            "mac": mac,
        }

        with self.condition:
            self.device_info[device_id] = info
            self.condition.notify_all()

        print()
        print(f"[INFO] {device_id}")
        print(f"       IP       : {ip}")
        print(f"       MAC      : {mac}")
        print(
            f"       firmware : "
            f"{info['firmware_version']}"
        )
        print(f"       slot     : {info['ota_slot']}")

    def process_ready(self, device_id, payload):
        received_config_id = payload.get("config_id")

        if received_config_id != self.config_id:
            return

        applied = payload.get("applied", {})
        expected = self.cfg["sensing"]
        errors = []

        if applied.get("band") != expected["band"]:
            errors.append(
                f"band={applied.get('band')} "
                f"(esperado={expected['band']})"
            )

        expected_channel = int(
            expected.get("expected_channel", 0)
        )
        actual_channel = applied.get("channel")

        if (
            expected_channel > 0
            and actual_channel != expected_channel
        ):
            errors.append(
                f"channel={actual_channel} "
                f"(esperado={expected_channel})"
            )

        if (
            applied.get("bandwidth_mhz")
            != expected["bandwidth_mhz"]
        ):
            errors.append(
                f"bandwidth="
                f"{applied.get('bandwidth_mhz')} "
                f"(esperado={expected['bandwidth_mhz']})"
            )

        if applied.get("phy") != expected["phy"]:
            errors.append(
                f"phy={applied.get('phy')} "
                f"(esperado={expected['phy']})"
            )

        if errors:
            print()
            print(
                f"[ERROR] {device_id}: "
                "configuración incorrecta"
            )
            for error in errors:
                print(f"        - {error}")
            return

        network = payload.get("network", {})

        with self.condition:
            self.ready_devices.add(device_id)
            expected_ready = set(
                self.ready_expected_devices
            )
            ready_count = len(
                self.ready_devices & expected_ready
            )
            ready_total = len(expected_ready)
            self.condition.notify_all()

        print()
        print(f"[READY] {device_id}")
        print(
            f"        banda   : "
            f"{applied.get('band')}"
        )
        print(f"        canal   : {actual_channel}")
        print(
            f"        BW      : "
            f"{applied.get('bandwidth_mhz')} MHz"
        )
        print(
            f"        PHY     : "
            f"{applied.get('phy')}"
        )
        print(
            f"        RSSI    : "
            f"{network.get('rssi')} dBm"
        )
        print(
            f"        READY   : "
            f"{ready_count}/{ready_total}"
        )

    def process_armed(self, device_id, payload):
        if payload.get("config_id") != self.config_id:
            return

        if payload.get("session_id") != self.session_id:
            return

        if payload.get("block_id") != self.current_block_id:
            return

        with self.condition:
            self.armed_devices.add(device_id)
            expected = set(self.capture_expected_devices)
            count = len(self.armed_devices & expected)
            total = len(expected)
            self.condition.notify_all()

        print(
            f"[ARMED] {device_id} "
            f"({count}/{total})"
        )

    def process_csi_complete(self, device_id, payload):
        if payload.get("config_id") != self.config_id:
            return

        if payload.get("session_id") != self.session_id:
            return

        block_id = payload.get("block_id")
        if block_id != self.current_block_id:
            return

        with self.condition:
            self.csi_complete_devices.add(
                device_id
            )
            self.csi_complete_payloads[
                (
                    self.config_id,
                    self.session_id,
                    device_id,
                    block_id,
                )
            ] = payload

            frames = payload.get("frames", 0)
            target = payload.get(
                "target_frames",
                0,
            )
            chunks = payload.get("chunks", 0)

            coverage = (
                frames / target * 100.0
                if target
                else 0.0
            )

            print(
                f"[CSI_COMPLETE] {device_id} "
                f"block={block_id} "
                f"frames={frames}/{target} "
                f"({coverage:.2f} %) "
                f"chunks={chunks}"
            )

            self.condition.notify_all()

    def process_health_result(self, payload):
        device_id = payload.get("device_id")
        config_id = payload.get("config_id")
        session_id = payload.get("session_id")
        block_id = payload.get("block_id")

        if device_id not in self.devices:
            return

        if not isinstance(config_id, int):
            return
        if not isinstance(block_id, int):
            return
        if not isinstance(session_id, str):
            return

        key = (
            config_id,
            session_id,
            device_id,
            block_id,
        )

        with self.condition:
            self.health_results[key] = payload
            self.condition.notify_all()

        if session_id.startswith("health_"):
            print(
                f"[HEALTH] {device_id}: "
                f"{payload.get('status')} "
                f"frames={payload.get('frame_count')} "
                f"unique={payload.get('unique_vectors')} "
                f"ratio={float(payload.get('unique_ratio', 0.0)) * 100:.2f}%"
            )
        else:
            print(
                f"[QUALITY] {device_id} block={block_id}: "
                f"signal={payload.get('status')} "
                f"quality={payload.get('quality_status')} "
                f"model_ready={payload.get('model_ready')} "
                f"coverage={float(payload.get('coverage', 0.0)) * 100:.2f}%"
            )

    def process_block_decision_ack(self, payload):
        config_id = payload.get("config_id")
        session_id = payload.get("session_id")
        block_id = payload.get("block_id")
        action = payload.get("action")

        if not isinstance(config_id, int):
            return
        if not isinstance(session_id, str):
            return
        if not isinstance(block_id, int):
            return
        if action not in {"accept", "discard"}:
            return

        attempt = payload.get("attempt")
        if not isinstance(attempt, int):
            return

        key = (config_id, session_id, block_id, action, attempt)
        with self.condition:
            self.block_decision_acks[key] = payload
            self.condition.notify_all()

        state = "OK" if payload.get("success") else "ERROR"
        print(
            f"[DECISION] ACK {action.upper()} "
            f"block={block_id} intento={attempt}: {state}"
        )

    def publish_json(
        self,
        topic,
        payload,
        qos=1,
        retain=False,
    ):
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
        )

        info = self.client.publish(
            topic,
            encoded,
            qos=qos,
            retain=retain,
        )
        info.wait_for_publish()

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(
                f"Error publicando "
                f"{topic}: {info.rc}"
            )

    def build_config(self):
        return {
            "schema_version": self.cfg["schema_version"],
            "config_id": self.config_id,
            "experiment_id": self.experiment_id,
            "sensing": copy.deepcopy(
                self.cfg["sensing"]
            ),
            "probe": copy.deepcopy(
                self.cfg["probe"]
            ),
            "capture": copy.deepcopy(
                self.cfg["capture"]
            ),
            "upload": copy.deepcopy(
                self.cfg["upload"]
            ),
        }

    def publish_control_config(self, device_payload):


        control_payload = copy.deepcopy(
            device_payload
        )
        control_payload["purpose"] = (
            "healthcheck"
            if self.session_id.startswith("health_")
            else "experiment"
        )
        control_payload["session_id"] = self.session_id
        control_payload["healthcheck"] = copy.deepcopy(
            self.cfg.get("healthcheck", {})
        )

        self.publish_json(
            f"{BASE_TOPIC}/control/config",
            control_payload,
            qos=1,
            retain=True,
        )

    def send_config(
        self,
        devices=None,
        *,
        clear_ready=True,
        show_payload=True,
    ):
        payload = self.build_config()

        target_devices = set(
            devices if devices is not None else self.devices
        )

        if not target_devices:
            return

        unknown = target_devices - self.devices
        if unknown:
            raise RuntimeError(
                "Intento de CONFIG a receptores "
                "no seleccionados: "
                + ", ".join(sorted(unknown))
            )

        self.ready_expected_devices = set(
            target_devices
        )

        if clear_ready:
            with self.condition:
                self.ready_devices.difference_update(
                    target_devices
                )


        self.publish_control_config(payload)

        if show_payload:
            print()
            print("======================================")
            title = (
                " CONFIGURACION DE HEALTHCHECK"
                if self.session_id.startswith("health_")
                else " CONFIGURACION DEL EXPERIMENTO"
            )
            print(title)
            print("======================================")
            print(
                json.dumps(
                    payload,
                    indent=2,
                    ensure_ascii=False,
                )
            )
            print()

        for device_id in sorted(target_devices):
            topic = (
                f"{BASE_TOPIC}/devices/"
                f"{device_id}/cmd/config"
            )
            self.publish_json(
                topic,
                payload,
                qos=1,
                retain=True,
            )
            print(
                f"[SERVER] CONFIG -> {device_id}"
            )

        if show_payload:
            print(
                "[SERVER] CONFIG publicado sólo "
                "a receptores seleccionados"
            )


    def send_arm(self, block_id, devices=None):
        target_devices = set(
            devices if devices is not None else self.devices
        )
        if not target_devices:
            raise RuntimeError(
                "No hay receptores para ARM"
            )

        self.current_block_id = int(block_id)
        self.capture_expected_devices = set(
            target_devices
        )

        payload = {
            "session_id": self.session_id,
            "block_id": self.current_block_id,
            "config_id": self.config_id,
        }

        with self.condition:
            self.armed_devices.clear()
            self.csi_complete_devices.clear()

        print()

        for device_id in sorted(target_devices):
            topic = (
                f"{BASE_TOPIC}/devices/"
                f"{device_id}/cmd/arm"
            )
            self.publish_json(
                topic,
                payload,
                qos=1,
                retain=False,
            )
            print(
                f"[SERVER] ARM -> {device_id}"
            )

        print(
            "[SERVER] ARM enviado para "
            f"block_id={self.current_block_id}"
        )

    def wait_for_device_info(
        self,
        devices=None,
        timeout=None,
    ):
        expected = set(
            devices if devices is not None else self.devices
        )
        timeout = float(
            self.info_timeout_s
            if timeout is None
            else timeout
        )
        deadline = time.monotonic() + timeout
        last_missing = None

        with self.condition:
            while True:
                missing = expected - set(self.device_info)
                if not missing:
                    return

                if missing != last_missing:
                    print(
                        "[MQTT] Esperando INFO fresco de: "
                        + ", ".join(sorted(missing))
                    )
                    last_missing = set(missing)

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "Falta INFO MQTT después de esperar "
                        f"{timeout:.1f}s: "
                        + ", ".join(sorted(missing))
                        + ". Los RX pueden estar asociados al AP "
                          "pero todavía sin haber repubicado su "
                          "inventario MQTT tras la reasociación."
                    )

                self.condition.wait(timeout=remaining)

    def build_probe_generator_for_devices(
        self,
        devices,
    ):
        target_devices = set(devices)
        self.wait_for_device_info(
            target_devices
        )

        targets = {}
        seen_ips = set()
        seen_macs = set()

        for device_id in sorted(target_devices):
            info = self.device_info[device_id]
            ip = info["ip"]
            mac = info["mac"]

            if ip in seen_ips:
                raise RuntimeError(
                    f"IP duplicada detectada: {ip}"
                )
            if mac in seen_macs:
                raise RuntimeError(
                    f"MAC duplicada detectada: {mac}"
                )

            seen_ips.add(ip)
            seen_macs.add(mac)
            targets[device_id] = ip

        target_host = ",".join(
            targets[device_id]
            for device_id in sorted(targets)
        )

        generator = ProbeGenerator(
            target_host=target_host,
            target_port=UDP_TARGET_PORT,
            broadcast=UDP_BROADCAST,
        )

        return generator, targets

    def prepare_probe_generator(self):
        (
            self.probe_generator,
            self.udp_targets,
        ) = self.build_probe_generator_for_devices(
            self.devices
        )

        print()
        print("======================================")
        print(" DESTINOS UDP RESUELTOS POR MQTT")
        print("======================================")

        for device_id in sorted(
            self.udp_targets
        ):
            info = self.device_info[device_id]
            print(
                f"{device_id:<5} "
                f"{self.udp_targets[device_id]}"
                f":{UDP_TARGET_PORT} "
                f"MAC={info['mac']}"
            )

        print("======================================")


    def wait_for_devices(
        self,
        expected_devices,
        state_set,
        timeout,
        description,
    ):
        expected_devices = set(expected_devices)
        deadline = (
            time.monotonic()
            + float(timeout)
        )

        with self.condition:
            while not expected_devices.issubset(
                state_set
            ):
                remaining = (
                    deadline - time.monotonic()
                )

                if remaining <= 0:
                    missing = (
                        expected_devices - state_set
                    )
                    raise TimeoutError(
                        f"Timeout esperando "
                        f"{description}. Faltan: "
                        f"{', '.join(sorted(missing))}"
                    )

                self.condition.wait(
                    timeout=remaining
                )

    def wait_for_all(
        self,
        state_set,
        timeout,
        description,
    ):
        self.wait_for_devices(
            self.devices,
            state_set,
            timeout,
            description,
        )

    def wait_for_health_results(
        self,
        devices,
        *,
        config_id,
        session_id,
        block_id,
        timeout,
    ):
        expected = set(devices)
        deadline = time.monotonic() + float(timeout)

        def collect_locked():
            found = {}
            for device_id in expected:
                key = (
                    int(config_id),
                    str(session_id),
                    device_id,
                    int(block_id),
                )
                payload = self.health_results.get(key)
                if payload is not None:
                    found[device_id] = payload
            return found

        with self.condition:
            while True:
                found = collect_locked()
                if set(found) == expected:
                    return found

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = expected - set(found)
                    raise TimeoutError(
                        "Timeout esperando resultados CSI/health de: "
                        + ", ".join(sorted(missing))
                        + f" (config={config_id}, session={session_id}, "
                          f"block={block_id})"
                    )

                self.condition.wait(timeout=remaining)


    def _recovery_udp_exchange(
        self,
        device_id,
        message,
        expected_nonce,
        accepted_states,
    ):
        info = self.device_info.get(device_id, {})
        ip = str(info.get("ip", "")).strip()

        try:
            ipaddress.ip_address(ip)
        except ValueError as exc:
            raise RuntimeError(
                f"{device_id}: IP inválida/no disponible para UDP recovery: {ip!r}"
            ) from exc

        timeout_s = max(
            0.1,
            RECOVERY_UDP_TIMEOUT_MS / 1000.0,
        )

        last_error = None

        for attempt in range(1, RECOVERY_UDP_RETRIES + 1):
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM,
            )
            sock.settimeout(timeout_s)

            try:
                sock.sendto(
                    message.encode("utf-8"),
                    (ip, RECOVERY_UDP_PORT),
                )

                raw, source = sock.recvfrom(1024)
                response = raw.decode(
                    "utf-8",
                    errors="replace",
                ).strip()

                parts = response.split("|")
                if len(parts) < 4:
                    raise RuntimeError(
                        f"respuesta UDP recovery inválida: {response!r}"
                    )

                magic, state, rx_device, nonce_text = parts[:4]

                if magic != "WSR1":
                    raise RuntimeError(
                        f"magic UDP recovery inválido: {magic!r}"
                    )
                if rx_device != device_id:
                    raise RuntimeError(
                        f"respuesta UDP de {rx_device}, esperaba {device_id}"
                    )
                if nonce_text != str(expected_nonce):
                    raise RuntimeError(
                        "nonce UDP recovery no coincide"
                    )
                if state not in accepted_states:
                    detail = parts[4] if len(parts) > 4 else ""
                    raise RuntimeError(
                        f"UDP recovery respondió {state} {detail}".strip()
                    )

                return {
                    "state": state,
                    "parts": parts,
                    "source": source,
                    "ip": ip,
                    "attempt": attempt,
                }

            except (socket.timeout, OSError, RuntimeError) as exc:
                last_error = exc

            finally:
                sock.close()

        raise TimeoutError(
            f"{device_id}: sin respuesta UDP recovery "
            f"en {ip}:{RECOVERY_UDP_PORT} después de "
            f"{RECOVERY_UDP_RETRIES} intentos: {last_error}"
        )

    def udp_recovery_ping(self, device_id):
        nonce = time.time_ns()
        message = f"WSR1|PING|{device_id}|{nonce}"

        result = self._recovery_udp_exchange(
            device_id,
            message,
            nonce,
            {"PONG"},
        )

        parts = result["parts"]

        parsed = {
            "version": parts[4] if len(parts) > 4 else "unknown",
            "uptime_ms": int(parts[5]) if len(parts) > 5 else None,
            "mqtt_connected": (
                parts[6] == "1" if len(parts) > 6 else None
            ),
            "wifi_connected": (
                parts[7] == "1" if len(parts) > 7 else None
            ),
            "rssi": int(parts[8]) if len(parts) > 8 else None,
            "channel": int(parts[9]) if len(parts) > 9 else None,
        }

        return parsed

    def udp_recovery_restart(self, device_id):
        _username, device_password = get_rx_credentials(device_id)

        nonce = time.time_ns()
        signed_message = (
            f"WSR1|RESTART|{device_id}|{nonce}"
        )
        signature = hmac.new(
            device_password.encode("utf-8"),
            signed_message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        message = f"{signed_message}|{signature}"

        result = self._recovery_udp_exchange(
            device_id,
            message,
            nonce,
            {"RESTARTING", "BUSY"},
        )

        if result["state"] == "BUSY":
            parts = result["parts"]
            detail = parts[4] if len(parts) > 4 else ""
            if detail == "restart_pending":
                print(
                    f"[RECOVERY-UDP] {device_id}: "
                    "reinicio ya estaba pendiente"
                )
                return

            raise RuntimeError(
                f"{device_id}: UDP RESTART rechazado ({detail or 'busy'})"
            )

        print(
            f"[RECOVERY-UDP] {device_id}: "
            "RESTARTING confirmado por UDP"
        )

    def request_udp_recovery_restart(self, devices):
        devices = set(devices)
        failures = {}

        print(
            "[RECOVERY-UDP] MQTT no confirmó RESTARTING; "
            "activando canal UDP independiente."
        )

        for device_id in sorted(devices):
            try:
                pong = self.udp_recovery_ping(device_id)

                print(
                    f"[RECOVERY-UDP] PONG {device_id}: "
                    f"WiFi={'OK' if pong['wifi_connected'] else 'NO'} "
                    f"MQTT={'OK' if pong['mqtt_connected'] else 'OFFLINE'} "
                    f"RSSI={pong['rssi']} dBm "
                    f"ch={pong['channel']}"
                )

                self.udp_recovery_restart(device_id)

                with self.condition:
                    self.restart_ack_devices.add(device_id)
                    self.online_devices.discard(device_id)
                    self.ready_devices.discard(device_id)
                    self.condition.notify_all()

            except Exception as exc:
                failures[device_id] = str(exc)

        if failures:
            details = "; ".join(
                f"{device}: {error}"
                for device, error in sorted(failures.items())
            )
            raise RuntimeError(
                "Fallback UDP de recuperación falló: " + details
            )

    def publish_restart(
        self,
        device_id,
        reason="ready_timeout",
    ):
        topic = (
            f"{BASE_TOPIC}/devices/"
            f"{device_id}/cmd/restart"
        )

        self.publish_json(
            topic,
            {
                "reason": reason,
                "config_id": self.config_id,
            },
            qos=1,
            retain=False,
        )

        print(
            f"[RECOVERY] RESTART -> "
            f"{device_id} ({reason})"
        )

    def request_restart_with_ack(
        self,
        devices,
        reason="ready_timeout",
    ):
        devices = set(devices)

        with self.condition:
            self.restart_ack_devices.difference_update(
                devices
            )
            self.restart_rejected_devices.difference_update(
                devices
            )
            self.online_devices.difference_update(
                devices
            )
            self.ready_devices.difference_update(
                devices
            )

        pending = set(devices)

        for attempt in range(
            1,
            self.restart_command_retries + 1,
        ):
            print(
                f"[RECOVERY] Intento de reinicio "
                f"{attempt}/"
                f"{self.restart_command_retries}: "
                + ", ".join(sorted(pending))
            )

            for device_id in sorted(pending):
                self.publish_restart(
                    device_id,
                    reason=reason,
                )

            deadline = (
                time.monotonic()
                + self.restart_ack_timeout_s
            )

            with self.condition:
                while True:
                    rejected = (
                        devices
                        & self.restart_rejected_devices
                    )

                    if rejected:
                        raise RuntimeError(
                            "RESTART rechazado por: "
                            + ", ".join(
                                sorted(rejected)
                            )
                        )

                    pending = (
                        devices
                        - self.restart_ack_devices
                    )
                    if not pending:
                        return

                    remaining = (
                        deadline - time.monotonic()
                    )
                    if remaining <= 0:
                        break

                    self.condition.wait(
                        timeout=remaining
                    )

            pending = (
                devices
                - self.restart_ack_devices
            )

        pending = (
            devices
            - self.restart_ack_devices
        )

        if not pending:
            return

        if not RECOVERY_UDP_ENABLED:
            raise TimeoutError(
                "No se confirmó RESTARTING de: "
                + ", ".join(sorted(pending))
                + ". UDP recovery está deshabilitado."
            )

        self.request_udp_recovery_restart(pending)

    def restart_and_recover(
        self,
        devices,
        reason="ready_timeout",
    ):
        devices = set(devices)
        if not devices:
            return

        print()
        print("======================================")
        print(" RECUPERACION AUTOMATICA")
        print("======================================")
        print(
            "Receptores         : "
            + ", ".join(sorted(devices))
        )

        self.wait_for_associated_devices(
            devices=devices,
            timeout=self.association_timeout_s,
        )

        self.request_restart_with_ack(
            devices,
            reason=reason,
        )

        print(
            "[RECOVERY] Esperando nuevo ONLINE..."
        )
        self.wait_for_devices(
            devices,
            self.online_devices,
            self.restart_online_timeout_s,
            "ONLINE después de RESTART",
        )

        print(
            "[RECOVERY] Esperando asociación "
            "después del boot..."
        )
        status = self.wait_for_associated_devices(
            devices=devices,
            timeout=self.association_timeout_s,
        )

        for device_id in sorted(devices):
            item = status[device_id]
            signal = item.get("signal_dbm")
            signal_text = (
                f"{signal} dBm"
                if signal is not None
                else "n/d"
            )
            print(
                f"[AP] {device_id} asociado "
                f"MAC={item['mac']} "
                f"RSSI={signal_text}"
            )

        print(
            "[RECOVERY] Reenviando CONFIG..."
        )
        self.send_config(
            devices=devices,
            clear_ready=False,
            show_payload=False,
        )

        self.ready_expected_devices = set(
            devices
        )
        self.wait_for_devices(
            devices,
            self.ready_devices,
            self.restart_ready_timeout_s,
            "READY después de RESTART",
        )

        print(
            "[RECOVERY] Recuperación completada."
        )

    def request_csi_soft_reinit(self, devices, reason):
        devices = set(devices)
        if not devices:
            return

        request_id = self._new_config_id()

        with self.condition:
            for device_id in devices:
                self.csi_reinit_ack_ids.pop(device_id, None)
                self.csi_reinit_rejected.pop(device_id, None)

        print(
            "[CSI-RECOVERY] Soft-reinit -> "
            + ", ".join(sorted(devices))
        )

        for device_id in sorted(devices):
            topic = (
                f"{BASE_TOPIC}/devices/{device_id}/cmd/csi-reinit"
            )
            self.publish_json(
                topic,
                {
                    "request_id": request_id,
                    "reason": reason,
                    "config_id": self.config_id,
                },
                qos=1,
                retain=False,
            )

        deadline = time.monotonic() + self.health_reinit_ack_timeout_s

        with self.condition:
            while True:
                rejected = {
                    device_id: self.csi_reinit_rejected.get(device_id)
                    for device_id in devices
                    if self.csi_reinit_rejected.get(device_id, {}).get(
                        "request_id"
                    ) == request_id
                }
                if rejected:
                    detail = "; ".join(
                        f"{device_id}: {item.get('detail')}"
                        for device_id, item in sorted(rejected.items())
                    )
                    raise RuntimeError(
                        "CSI soft-reinit rechazado: " + detail
                    )

                pending = {
                    device_id
                    for device_id in devices
                    if self.csi_reinit_ack_ids.get(device_id) != request_id
                }
                if not pending:
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Timeout esperando CSI_REINIT_DONE de: "
                        + ", ".join(sorted(pending))
                    )
                self.condition.wait(timeout=remaining)

        if self.health_reinit_pause_ms > 0:
            time.sleep(self.health_reinit_pause_ms / 1000.0)

    def _make_health_cfg(self):
        health_cfg = copy.deepcopy(self.cfg)
        health_cfg["probe"]["rate_hz"] = self.health_rate_hz

        duration_ms = int(
            round(
                self.health_search_target_packets
                / self.health_rate_hz
                * 1000.0
            )
        )

        health_cfg["capture"]["blocks"] = 1
        health_cfg["capture"]["target_packets"] = (
            self.health_search_target_packets
        )
        health_cfg["capture"]["nominal_duration_ms"] = duration_ms
        health_cfg["capture"]["max_capture_ms"] = max(
            self.health_max_capture_ms,
            duration_ms + 250,
        )
        health_cfg["capture"]["settle_time_ms"] = (
            self.health_search_settle_time_ms
        )
        return health_cfg

    def _health_result_is_good(self, result):
        try:
            ratio = float(result.get("unique_ratio", 0.0))
            frames = int(result.get("frame_count", 0))
        except (TypeError, ValueError):
            return False

        return (
            result.get("status") == "PASS"
            and frames >= self.health_min_frames
            and ratio >= self.health_good_unique_ratio
        )

    def run_fast_health_probe(
        self,
        devices,
        *,
        block_id,
    ):
        devices = set(devices)
        generator, _targets = self.build_probe_generator_for_devices(
            devices
        )

        with self.condition:
            for device_id in devices:
                self.health_results.pop(
                    (
                        self.config_id,
                        self.session_id,
                        device_id,
                        block_id,
                    ),
                    None,
                )

        self.send_arm(block_id, devices=devices)
        self.wait_for_devices(
            devices,
            self.armed_devices,
            float(self.cfg["timeouts"]["armed_s"]),
            "ARMED fast-health",
        )

        generator.run_block(
            config_id=self.config_id,
            block_id=block_id,
            rate_hz=self.health_rate_hz,
            packet_count=self.health_search_target_packets,
        )

        duration_s = (
            self.health_search_target_packets / self.health_rate_hz
        )
        self.wait_for_devices(
            devices,
            self.csi_complete_devices,
            max(2.0, duration_s + 1.0),
            "CSI_COMPLETE fast-health",
        )

        return self.wait_for_health_results(
            devices,
            config_id=self.config_id,
            session_id=self.session_id,
            block_id=block_id,
            timeout=self.health_result_timeout_s,
        )

    def find_and_lock_good_csi_state(
        self,
        devices,
        *,
        reason,
        restore_same_formal_id,
    ):
        target_devices = set(devices)
        if not target_devices:
            return

        real_cfg = self.cfg
        real_config_id = self.config_id
        real_session_id = self.session_id
        real_current_block_id = self.current_block_id

        health_cfg = self._make_health_cfg()
        pending = set(target_devices)
        good_streak = {device_id: 0 for device_id in target_devices}
        soft_counts = {device_id: 0 for device_id in target_devices}
        reboot_counts = {device_id: 0 for device_id in target_devices}
        total_soft = {device_id: 0 for device_id in target_devices}
        probe_number = 0

        print()
        print("======================================")
        print(" BUSQUEDA RAPIDA DE ESTADO CSI SANO")
        print("======================================")
        print("Receptores          : " + ", ".join(sorted(target_devices)))
        print(
            f"Probes/check         : {self.health_search_target_packets} "
            f"@ {self.health_rate_hz} Hz"
        )
        print(
            f"Criterio bueno       : unique_ratio >= "
            f"{self.health_good_unique_ratio * 100:.1f}%"
        )
        print(
            f"Validación           : {self.health_required_consecutive} "
            "checks consecutivos"
        )
        print(
            f"Soft-reinit/boot     : hasta {self.health_max_soft_reinits}"
        )
        print(
            f"Reboots completos    : hasta {self.health_max_full_restarts}"
        )

        try:
            self.cfg = health_cfg
            self.config_id = self._new_config_id()
            self.session_id = (
                f"health_{self.experiment_id}_fast_{int(time.time() * 1000)}"
            )
            self.current_block_id = 0


            self.send_config(
                devices=target_devices,
                clear_ready=True,
                show_payload=False,
            )
            self.wait_ready_with_recovery(target_devices)

            while pending:
                probe_number += 1

                print()
                print(
                    f"[HEALTH-FAST] check={probe_number} pending="
                    + ", ".join(sorted(pending))
                )

                results = self.run_fast_health_probe(
                    pending,
                    block_id=probe_number,
                )

                to_reinit = set()
                locked_now = set()

                for device_id in sorted(pending):
                    result = results[device_id]
                    ratio = float(result.get("unique_ratio", 0.0))
                    frames = int(result.get("frame_count", 0))
                    unique = int(result.get("unique_vectors", 0))
                    status = str(result.get("status", "UNKNOWN"))

                    if self._health_result_is_good(result):
                        good_streak[device_id] += 1
                        print(
                            f"[HEALTH-FAST] {device_id}: GOOD "
                            f"{unique}/{frames} "
                            f"({ratio * 100:.1f}%) "
                            f"streak={good_streak[device_id]}/"
                            f"{self.health_required_consecutive}"
                        )

                        if (
                            good_streak[device_id]
                            >= self.health_required_consecutive
                        ):
                            locked_now.add(device_id)
                    else:
                        good_streak[device_id] = 0
                        print(
                            f"[HEALTH-FAST] {device_id}: BAD "
                            f"status={status} unique={unique}/{frames} "
                            f"({ratio * 100:.1f}%)"
                        )
                        to_reinit.add(device_id)

                if locked_now:
                    for device_id in sorted(locked_now):
                        pending.discard(device_id)
                        print(
                            f"[HEALTH-FAST] {device_id}: LOCKED_GOOD; "
                            "no se vuelve a tocar CSI"
                        )

                if not pending:
                    break


                if to_reinit:
                    soft_group = set()
                    reboot_group = set()

                    for device_id in sorted(to_reinit):
                        if soft_counts[device_id] < self.health_max_soft_reinits:
                            soft_counts[device_id] += 1
                            total_soft[device_id] += 1
                            soft_group.add(device_id)
                        else:
                            if reboot_counts[device_id] >= self.health_max_full_restarts:
                                raise RuntimeError(
                                    f"No se encontró estado CSI sano para {device_id} "
                                    f"tras {total_soft[device_id]} soft-reinit y "
                                    f"{reboot_counts[device_id]} reboot(s)"
                                )
                            reboot_counts[device_id] += 1
                            soft_counts[device_id] = 0
                            reboot_group.add(device_id)

                    if soft_group:
                        self.request_csi_soft_reinit(
                            soft_group,
                            reason=reason,
                        )

                    if reboot_group:
                        print(
                            "[HEALTH-FAST] Agotados soft-reinit del boot; "
                            "reboot completo -> "
                            + ", ".join(sorted(reboot_group))
                        )
                        self.restart_and_recover(
                            reboot_group,
                            reason="csi_fast_search_exhausted",
                        )

                if self.health_between_checks_ms > 0:
                    time.sleep(self.health_between_checks_ms / 1000.0)

            print()
            print("======================================")
            print(" ESTADO CSI SANO BLOQUEADO")
            print("======================================")
            for device_id in sorted(target_devices):
                print(
                    f"{device_id}: LOCKED_GOOD "
                    f"soft_reinit={total_soft[device_id]} "
                    f"reboots={reboot_counts[device_id]}"
                )

        finally:
            self.cfg = real_cfg
            self.config_id = (
                real_config_id
                if restore_same_formal_id
                else self._new_config_id()
            )
            self.session_id = real_session_id
            self.current_block_id = real_current_block_id
            self.ready_expected_devices = set(self.devices)
            self.capture_expected_devices = set(self.devices)

            with self.condition:
                self.ready_devices.clear()
                self.armed_devices.clear()
                self.csi_complete_devices.clear()

    def validate_csi_before_experiment(self):
        if not self.health_enabled:
            print()
            print("[HEALTH] Precheck CSI deshabilitado.")
            return

        self.find_and_lock_good_csi_state(
            self.devices,
            reason="initial_fast_health_search",
            restore_same_formal_id=False,
        )

    def recover_frozen_csi_before_retry(self, devices):


        self.find_and_lock_good_csi_state(
            devices,
            reason="formal_block_frozen",
            restore_same_formal_id=True,
        )


        self.send_config(
            devices=set(devices),
            clear_ready=True,
            show_payload=False,
        )
        self.wait_ready_with_recovery(set(devices))

    def wait_ready_with_recovery(
        self,
        devices=None,
    ):
        expected_devices = set(
            devices if devices is not None else self.devices
        )
        self.ready_expected_devices = set(
            expected_devices
        )

        ready_timeout = float(
            self.cfg["timeouts"]["ready_s"]
        )

        try:
            self.wait_for_devices(
                expected_devices,
                self.ready_devices,
                ready_timeout,
                "READY",
            )
            return

        except TimeoutError:
            missing = (
                expected_devices
                - self.ready_devices
            )

        print()
        print(
            "[WARN] Timeout inicial esperando READY."
        )
        print(
            "[WARN] Faltan: "
            + ", ".join(sorted(missing))
        )

        print(
            "[RECOVERY] Verificando asociación "
            "antes de reiniciar..."
        )

        try:
            self.wait_for_associated_devices(
                devices=missing,
                timeout=self.association_timeout_s,
            )
        except TimeoutError as exc:
            print(f"[WARN] {exc}")
            print(
                "[WARN] Sin asociación no es posible "
                "enviar un reinicio remoto por MQTT."
            )
            raise

        print(
            "[RECOVERY] Reintentando CONFIG "
            "sin reiniciar: "
            + ", ".join(sorted(missing))
        )

        self.send_config(
            devices=missing,
            clear_ready=True,
            show_payload=False,
        )

        try:
            self.wait_for_devices(
                missing,
                self.ready_devices,
                self.ready_retry_timeout_s,
                "READY después de reintentar CONFIG",
            )
            print(
                "[RECOVERY] READY recuperado "
                "sin reiniciar."
            )
            return

        except TimeoutError:
            missing = (
                expected_devices
                - self.ready_devices
            )

        if not self.restart_enabled:
            raise TimeoutError(
                "READY no se recuperó y "
                "recovery.restart_enabled=false. "
                "Faltan: "
                + ", ".join(sorted(missing))
            )

        print(
            "[RECOVERY] CONFIG no fue suficiente; "
            "se reiniciará sólo: "
            + ", ".join(sorted(missing))
        )

        self.restart_and_recover(
            missing,
            reason="ready_timeout",
        )

        self.wait_for_devices(
            expected_devices,
            self.ready_devices,
            1.0,
            "READY final",
        )


    def publish_block_decision(
        self,
        *,
        block_id,
        action,
        attempt,
        reason=None,
    ):
        if action not in {"accept", "discard"}:
            raise ValueError("action debe ser accept o discard")

        payload = {
            "config_id": self.config_id,
            "session_id": self.session_id,
            "block_id": int(block_id),
            "action": action,
            "attempt": int(attempt),
            "devices": sorted(self.devices),
            "reason": reason,
        }

        key = (
            self.config_id,
            self.session_id,
            int(block_id),
            action,
            int(attempt),
        )
        with self.condition:
            self.block_decision_acks.pop(key, None)

        self.publish_json(
            f"{BASE_TOPIC}/control/block-decision",
            payload,
            qos=1,
            retain=False,
        )

        deadline = (
            time.monotonic()
            + self.block_decision_timeout_s
        )
        with self.condition:
            while key not in self.block_decision_acks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timeout esperando ACK de {action} "
                        f"para block={block_id}"
                    )
                self.condition.wait(timeout=remaining)

            ack = self.block_decision_acks.pop(key)

        if not ack.get("success"):
            raise RuntimeError(
                f"csi_receiver rechazó decisión {action} "
                f"para block={block_id}: "
                f"{ack.get('detail')}"
            )

        return ack


    def connect(self):
        print(
            f"[SERVER] Conectando MQTT a "
            f"{self.broker_host}:"
            f"{self.broker_port}..."
        )

        self.client.connect(
            self.broker_host,
            self.broker_port,
            keepalive=30,
        )
        self.client.loop_start()

        timeout = float(
            self.cfg["timeouts"]["mqtt_connect_s"]
        )
        if not self.connected.wait(timeout):
            raise TimeoutError(
                "No se pudo conectar al broker MQTT"
            )

    def run_real_block(
        self,
        block_id,
        *,
        rate_hz,
        packet_count,
        armed_timeout,
        csi_complete_timeout,
    ):
        for attempt in range(1, self.block_max_attempts + 1):
            print()
            print("======================================")
            total_label = (
                "LIVE" if self.live_mode
                else str(self.cfg["capture"]["blocks"])
            )
            print(
                f" BLOQUE {block_id}/{total_label} "
                f"- INTENTO {attempt}/{self.block_max_attempts}"
            )
            print("======================================")


            with self.condition:
                for device_id in self.devices:
                    self.health_results.pop(
                        (
                            self.config_id,
                            self.session_id,
                            device_id,
                            block_id,
                        ),
                        None,
                    )
                    self.csi_complete_payloads.pop(
                        (
                            self.config_id,
                            self.session_id,
                            device_id,
                            block_id,
                        ),
                        None,
                    )

            self.send_arm(
                block_id,
                devices=self.devices,
            )

            print("[SERVER] Esperando ARMED...")
            self.wait_for_devices(
                self.devices,
                self.armed_devices,
                armed_timeout,
                f"ARMED block={block_id}",
            )

            print(
                f"[SERVER] Todos ARMED para block={block_id}"
            )

            self.probe_generator.run_block(
                config_id=self.config_id,
                block_id=block_id,
                rate_hz=rate_hz,
                packet_count=packet_count,
            )

            print()
            print("[SERVER] Esperando CSI_COMPLETE...")
            self.wait_for_devices(
                self.devices,
                self.csi_complete_devices,
                csi_complete_timeout,
                f"CSI_COMPLETE block={block_id}",
            )

            validations = self.wait_for_health_results(
                self.devices,
                config_id=self.config_id,
                session_id=self.session_id,
                block_id=block_id,
                timeout=self.health_result_timeout_s,
            )

            rejected = {
                device_id: result
                for device_id, result in validations.items()
                if not bool(result.get("accepted", False))
            }

            if not rejected:
                self.publish_block_decision(
                    block_id=block_id,
                    action="accept",
                    attempt=attempt,
                    reason=None,
                )

                qualities = ", ".join(
                    f"{device_id}={validations[device_id].get('quality_status')}"
                    for device_id in sorted(self.devices)
                )
                print(
                    f"[SERVER] Bloque {block_id} aceptado "
                    f"sincronizadamente ({qualities})"
                )
                return

            detail = ", ".join(
                f"{device_id}="
                f"{result.get('status')}/"
                f"{result.get('quality_status')}"
                for device_id, result in sorted(rejected.items())
            )

            print(
                f"[QUALITY] Bloque {block_id} rechazado: {detail}"
            )
            print(
                "[QUALITY] Se descarta este intento para TODOS "
                "los RX para conservar sincronización temporal."
            )

            self.publish_block_decision(
                block_id=block_id,
                action="discard",
                attempt=attempt,
                reason=detail,
            )

            frozen = {
                device_id
                for device_id, result in rejected.items()
                if result.get("status") == "CSI_FROZEN"
            }


            if attempt >= self.block_max_attempts:
                if frozen:
                    print(
                        "[RECOVERY] CSI_FROZEN detectado en el último "
                        "intento; se omite el reinicio porque no quedan "
                        "reintentos para este bloque."
                    )
                else:
                    print(
                        "[QUALITY] Último intento agotado; no quedan "
                        "reintentos para este bloque."
                    )

                raise RuntimeError(
                    f"Bloque {block_id} inválido después de "
                    f"{self.block_max_attempts} intento(s): {detail}"
                )

            if frozen:
                if not self.restart_enabled:
                    raise RuntimeError(
                        "CSI_FROZEN detectado, pero "
                        "recovery.restart_enabled=false: "
                        + ", ".join(sorted(frozen))
                    )

                print(
                    "[RECOVERY] CSI_FROZEN: búsqueda rápida de "
                    "nuevo estado sano -> "
                    + ", ".join(sorted(frozen))
                )
                self.recover_frozen_csi_before_retry(frozen)
            else:
                print(
                    "[QUALITY] No hay CSI_FROZEN: no se reinicia "
                    "ningún RX; sólo se repetirá la captura completa."
                )

            if self.block_retry_settle_time_ms > 0:
                print(
                    f"[QUALITY] Espera antes de repetir: "
                    f"{self.block_retry_settle_time_ms} ms"
                )
                time.sleep(
                    self.block_retry_settle_time_ms / 1000.0
                )

            print(
                f"[QUALITY] Repitiendo block={block_id} con "
                "TODOS los receptores."
            )


    def live_stop_requested(self) -> bool:
        return bool(
            self.live_mode
            and self.live_stop_file is not None
            and self.live_stop_file.exists()
        )

    def run(self):
        self.connect()

        try:

            self.configure_hotspot()

            print()
            print(
                "[SERVER] Esperando receptores "
                "asociados al AP..."
            )
            station_status = (
                self.wait_for_associated_devices()
            )

            for device_id in sorted(self.devices):
                item = station_status[device_id]
                signal = item.get("signal_dbm")
                signal_text = (
                    f"{signal} dBm"
                    if signal is not None
                    else "n/d"
                )
                print(
                    f"[AP] {device_id} asociado "
                    f"MAC={item['mac']} "
                    f"RSSI={signal_text}"
                )


            self.wait_for_device_info(
                self.devices,
                timeout=self.info_timeout_s,
            )


            self.validate_csi_before_experiment()


            if self.health_only:
                print()
                print("======================================")
                print(" HEALTHCHECK COMPLETADO")
                print("======================================")
                return


            print()
            print("======================================")
            print(" CONFIGURACION FORMAL")
            print("======================================")

            self.send_config(
                devices=self.devices,
                clear_ready=True,
                show_payload=True,
            )

            print()
            print(
                "[SERVER] Esperando READY formal..."
            )
            self.wait_ready_with_recovery(
                self.devices
            )

            print()
            print("======================================")
            print(" TODOS LOS RECEPTORES LISTOS")
            print("======================================")


            self.prepare_probe_generator()

            total_blocks = int(
                self.cfg["capture"]["blocks"]
            )
            rate_hz = float(
                self.cfg["probe"]["rate_hz"]
            )
            packet_count = int(
                self.cfg["capture"][
                    "target_packets"
                ]
            )
            armed_timeout = float(
                self.cfg["timeouts"]["armed_s"]
            )
            max_capture_s = (
                float(
                    self.cfg["capture"][
                        "max_capture_ms"
                    ]
                )
                / 1000.0
            )
            csi_complete_timeout = max(
                10.0,
                max_capture_s + 10.0,
            )

            print()
            print("======================================")
            print(
                " INICIO DE CAPTURA EN VIVO"
                if self.live_mode
                else " INICIO DE CAPTURA AUTOMATICA"
            )
            print("======================================")
            print(
                "Bloques          : continuo hasta detener"
                if self.live_mode
                else f"Bloques          : {total_blocks}"
            )
            print(
                f"Probes/bloque    : {packet_count}"
            )
            print(
                f"Tasa             : {rate_hz} Hz"
            )
            print(
                "Destinos UDP     : "
                + ", ".join(
                    f"{device}={ip}:"
                    f"{UDP_TARGET_PORT}"
                    for device, ip
                    in sorted(
                        self.udp_targets.items()
                    )
                )
            )

            completed_blocks = 0
            block_id = 1
            live_consecutive_failures = 0
            live_max_consecutive_failures = 3

            while True:
                if not self.live_mode and block_id > total_blocks:
                    break

                if self.live_stop_requested():
                    print(
                        "[LIVE] Detención solicitada antes del "
                        "siguiente bloque."
                    )
                    break

                try:
                    self.run_real_block(
                        block_id,
                        rate_hz=rate_hz,
                        packet_count=packet_count,
                        armed_timeout=armed_timeout,
                        csi_complete_timeout=(
                            csi_complete_timeout
                        ),
                    )

                except (TimeoutError, RuntimeError) as exc:
                    if not self.live_mode:
                        raise

                    live_consecutive_failures += 1
                    print()
                    print(
                        f"[LIVE-WARN] block={block_id} omitido: {exc}"
                    )
                    print(
                        "[LIVE-WARN] fallos consecutivos="
                        f"{live_consecutive_failures}/"
                        f"{live_max_consecutive_failures}"
                    )

                    if self.live_stop_requested():
                        print(
                            "[LIVE] Detención solicitada durante la "
                            "recuperación."
                        )
                        break

                    if (
                        live_consecutive_failures
                        >= live_max_consecutive_failures
                    ):
                        raise RuntimeError(
                            "Captura live abortada tras "
                            f"{live_consecutive_failures} fallos "
                            "consecutivos. Último error: "
                            f"{exc}"
                        ) from exc

                    print(
                        "[LIVE-RECOVERY] Resincronizando receptores "
                        "antes del siguiente bloque..."
                    )


                    self.send_config(
                        devices=self.devices,
                        clear_ready=True,
                        show_payload=False,
                    )
                    self.wait_ready_with_recovery(
                        self.devices
                    )
                    self.prepare_probe_generator()

                    settle_s = max(
                        0.25,
                        self.block_retry_settle_time_ms / 1000.0,
                    )
                    time.sleep(settle_s)


                    block_id += 1
                    continue

                completed_blocks = block_id
                live_consecutive_failures = 0

                if self.live_stop_requested():
                    print(
                        "[LIVE] Detención solicitada; bloque actual "
                        "finalizado correctamente."
                    )
                    break

                block_id += 1


            print()
            print("======================================")
            print(
                " CAPTURA EN VIVO FINALIZADA"
                if self.live_mode
                else " EXPERIMENTO COMPLETADO"
            )
            print("======================================")
            print(
                f"experiment_id : {self.experiment_id}"
            )
            print(
                f"session_id    : {self.session_id}"
            )
            print(
                f"config_id     : {self.config_id}"
            )
            print(f"blocks        : {completed_blocks}")

            print()
            print("Resumen CSI:")

            for block_id in range(
                1,
                completed_blocks + 1,
            ):
                for device_id in sorted(
                    self.devices
                ):
                    payload = (
                        self.csi_complete_payloads.get(
                            (
                                self.config_id,
                                self.session_id,
                                device_id,
                                block_id,
                            )
                        )
                    )
                    if payload is None:
                        continue

                    frames = payload.get(
                        "frames",
                        0,
                    )
                    target = payload.get(
                        "target_frames",
                        0,
                    )
                    coverage = (
                        frames / target * 100.0
                        if target
                        else 0.0
                    )

                    print(
                        f"  block={block_id:02d} "
                        f"{device_id}: "
                        f"{frames}/{target} "
                        f"({coverage:.2f} %)"
                    )

        finally:
            try:
                if self.client.is_connected():
                    self.client.disconnect()
            finally:
                self.client.loop_stop()


def load_experiment_config():
    path = (
        Path(__file__).resolve().parent
        / "experiment.json"
    )
    with open(
        path,
        "r",
        encoding="utf-8",
    ) as fh:
        return json.load(fh)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Controlador de experimentos "
            "WiFi Sensing"
        )
    )

    parser.add_argument(
        "--band",
        choices=("2.4g", "5g"),
        default=None,
        help=(
            "Sobrescribe la banda "
            "de experiment.json"
        ),
    )

    parser.add_argument(
        "--devices",
        default=None,
        help=(
            "Receptores separados por coma. "
            "Ejemplo: rx01 o rx01,rx02,rx03"
        ),
    )


    parser.add_argument(
        "--health-only",
        action="store_true",
        help="Ejecuta asociación + healthcheck CSI y termina antes de los bloques formales.",
    )


    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Captura bloques formalmente de forma continua hasta que "
            "aparezca --live-stop-file."
        ),
    )
    parser.add_argument(
        "--live-stop-file",
        default=None,
        help="Archivo centinela usado por la consola web para detener live.",
    )

    args = parser.parse_args()

    try:
        cfg = load_experiment_config()

        if args.health_only and args.live:
            raise RuntimeError(
                "--health-only y --live no pueden usarse juntos"
            )
        if args.live and not args.live_stop_file:
            raise RuntimeError(
                "--live requiere --live-stop-file"
            )

        if args.band is not None:
            cfg["sensing"]["band"] = args.band
            cfg["sensing"]["expected_channel"] = 0

        if args.devices:
            devices = [
                item.strip()
                for item in args.devices.split(",")
                if item.strip()
            ]
        else:
            devices = list(RX_DEVICES)

        if not devices:
            raise RuntimeError(
                "No se seleccionaron receptores"
            )

        if len(devices) != len(set(devices)):
            raise RuntimeError(
                "Hay receptores duplicados "
                "en --devices"
            )

        unknown = (
            set(devices) - set(RX_DEVICES)
        )
        if unknown:
            raise RuntimeError(
                "Receptores no definidos "
                "en RX_DEVICES: "
                + ", ".join(sorted(unknown))
            )

        print()
        print("======================================")
        print(" WIFI SENSING CONTROLLER")
        print("======================================")
        print(
            f"Banda       : "
            f"{cfg['sensing']['band']}"
        )
        print(
            "Receptores  : "
            + ", ".join(devices)
        )
        print("======================================")

        controller = ExperimentController(
            cfg,
            devices=devices,
            health_only=args.health_only,
            live_mode=args.live,
            live_stop_file=args.live_stop_file,
        )
        controller.run()

    except KeyboardInterrupt:
        print("\n[SERVER] Interrumpido.")

    except Exception as exc:
        print(f"\n[FATAL] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
