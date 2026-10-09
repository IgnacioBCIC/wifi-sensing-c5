from pathlib import Path
import os

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


if not ENV_FILE.exists():
    raise RuntimeError(
        f"No se encontró el archivo .env en: {ENV_FILE}"
    )

load_dotenv(ENV_FILE)


def get_required_env(name: str) -> str:

    value = os.getenv(name)

    if value is None or value.strip() == "":
        raise RuntimeError(
            f"Variable obligatoria no definida: {name}"
        )

    return value.strip()


def get_int_env(
    name: str,
    default: int,
) -> int:

    value = os.getenv(name)

    if value is None or value.strip() == "":
        return default

    try:
        return int(value)

    except ValueError as exc:
        raise RuntimeError(
            f"{name} debe ser un número entero"
        ) from exc


def get_bool_env(
    name: str,
    default: bool = False,
) -> bool:


    value = os.getenv(name)

    if value is None:
        return default

    normalized = value.strip().lower()

    if normalized in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False

    raise RuntimeError(
        f"{name} debe ser true/false"
    )


MQTT_HOST = get_required_env(
    "MQTT_HOST"
)

MQTT_PORT = get_int_env(
    "MQTT_PORT",
    1883,
)

MQTT_SERVER_USER = get_required_env(
    "MQTT_SERVER_USER"
)

MQTT_SERVER_PASSWORD = get_required_env(
    "MQTT_SERVER_PASSWORD"
)

MQTT_BASE_TOPIC = os.getenv(
    "MQTT_BASE_TOPIC",
    "wifi-sensing/v1",
).strip()


RX_DEVICES = [
    device.strip()
    for device in os.getenv(
        "RX_DEVICES",
        "rx01,rx02,rx03",
    ).split(",")
    if device.strip()
]


RX_BASE_MAC = {
    device: os.getenv(
        f"{device.upper()}_BASE_MAC",
        "",
    ).strip().lower()
    for device in RX_DEVICES
}


def get_rx_credentials(
    device_id: str,
) -> tuple[str, str]:

    key = device_id.upper()

    username = get_required_env(
        f"MQTT_{key}_USER"
    )

    password = get_required_env(
        f"MQTT_{key}_PASSWORD"
    )

    return username, password


WIFI_AP_INTERFACE = get_required_env(
    "WIFI_AP_INTERFACE"
)

WIFI_AP_SSID = get_required_env(
    "WIFI_AP_SSID"
)

WIFI_AP_IPV4_CIDR = get_required_env(
    "WIFI_AP_IPV4_CIDR"
)

WIFI_AP_PROFILE_24 = get_required_env(
    "WIFI_AP_PROFILE_24"
)

WIFI_AP_CHANNEL_24 = get_int_env(
    "WIFI_AP_CHANNEL_24",
    6,
)

WIFI_AP_PROFILE_5 = get_required_env(
    "WIFI_AP_PROFILE_5"
)

WIFI_AP_CHANNEL_5 = get_int_env(
    "WIFI_AP_CHANNEL_5",
    48,
)

WIFI_AP_BANDWIDTH_MHZ = get_int_env(
    "WIFI_AP_BANDWIDTH_MHZ",
    20,
)

WIFI_AP_VERIFY_TIMEOUT_S = get_int_env(
    "WIFI_AP_VERIFY_TIMEOUT_S",
    10,
)

WIFI_AP_ACTIVATION_RETRIES = get_int_env(
    "WIFI_AP_ACTIVATION_RETRIES",
    3,
)

WIFI_AP_RETRY_DELAY_S = get_int_env(
    "WIFI_AP_RETRY_DELAY_S",
    3,
)

if WIFI_AP_ACTIVATION_RETRIES < 1:
    raise RuntimeError(
        "WIFI_AP_ACTIVATION_RETRIES debe ser >= 1"
    )

if WIFI_AP_RETRY_DELAY_S < 0:
    raise RuntimeError(
        "WIFI_AP_RETRY_DELAY_S debe ser >= 0"
    )


UDP_TARGET_PORT = get_int_env(
    "UDP_TARGET_PORT",
    5005,
)

UDP_BROADCAST = get_bool_env(
    "UDP_BROADCAST",
    False,
)

CSI_UDP_PORT = get_int_env(
    "CSI_UDP_PORT",
    5007,
)


RECOVERY_UDP_ENABLED = get_bool_env(
    "RECOVERY_UDP_ENABLED",
    True,
)

RECOVERY_UDP_PORT = get_int_env(
    "RECOVERY_UDP_PORT",
    5006,
)

RECOVERY_UDP_TIMEOUT_MS = get_int_env(
    "RECOVERY_UDP_TIMEOUT_MS",
    1200,
)

RECOVERY_UDP_RETRIES = get_int_env(
    "RECOVERY_UDP_RETRIES",
    3,
)


SERVER_NAME = os.getenv(
    "SERVER_NAME",
    "wifi-sensing-server",
).strip()
