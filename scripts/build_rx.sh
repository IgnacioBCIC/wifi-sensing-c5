#!/usr/bin/env bash

set -euo pipefail


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FW_DIR="$ROOT/firmware/esp32-rx"
ENV_FILE="$ROOT/.env"


if [[ $# -ne 2 ]]; then
    echo "Uso:"
    echo "  $0 <rx01|rx02|rx03> <version>"
    echo
    echo "Ejemplo:"
    echo "  $0 rx01 1.0.2"
    exit 1
fi


DEVICE="$1"
VERSION="$2"


case "$DEVICE" in
    rx01|rx02|rx03)
        ;;
    *)
        echo "[ERROR] Dispositivo inválido: $DEVICE"
        exit 1
        ;;
esac


if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "[ERROR] Versión inválida: $VERSION"
    echo "Formato esperado: X.Y.Z"
    exit 1
fi


if [[ ! -f "$ENV_FILE" ]]; then
    echo "[ERROR] No existe $ENV_FILE"
    exit 1
fi


set -a
source "$ENV_FILE"
set +a


WS_IDF_PATH="${WS_IDF_PATH:-$HOME/esp/esp-idf-v6.1}"

if [[ ! -f "$WS_IDF_PATH/export.sh" ]]; then
    echo "[ERROR] No existe ESP-IDF 6.1 en:"
    echo "        $WS_IDF_PATH"
    echo "Puedes definir WS_IDF_PATH en .env."
    exit 1
fi

source "$WS_IDF_PATH/export.sh" >/dev/null

IDF_VERSION="$(idf.py --version 2>&1 | head -n 1)"

if [[ "$IDF_VERSION" != *"v6.1"* && "$IDF_VERSION" != *"6.1."* ]]; then
    echo "[ERROR] Esta V2 requiere ESP-IDF 6.1.x"
    echo "        detectado: $IDF_VERSION"
    exit 1
fi

echo "[OK] ESP-IDF fijado: $IDF_VERSION"
echo "[OK] IDF_PATH=$IDF_PATH"


DEVICE_UPPER="${DEVICE^^}"

USER_VAR="MQTT_${DEVICE_UPPER}_USER"
PASS_VAR="MQTT_${DEVICE_UPPER}_PASSWORD"

MQTT_USER="${!USER_VAR:-}"
MQTT_PASSWORD="${!PASS_VAR:-}"


if [[ -z "$MQTT_USER" || -z "$MQTT_PASSWORD" ]]; then
    echo "[ERROR] Credenciales MQTT faltantes para $DEVICE"
    exit 1
fi


AP_HOST="${OTA_HOST:-${WIFI_AP_IPV4_CIDR%/*}}"

OTA_PORT_VALUE="${OTA_PORT:-8000}"


if [[ -z "${WIFI_AP_SSID:-}" ]]; then
    echo "[ERROR] WIFI_AP_SSID no definido"
    exit 1
fi


if [[ -z "${WIFI_AP_PASSWORD:-}" ]]; then
    echo "[ERROR] WIFI_AP_PASSWORD no definido"
    exit 1
fi


BUILD_DIR="$FW_DIR/build/$DEVICE"

SDKCONFIG_FILE="$BUILD_DIR/sdkconfig"

LOCAL_DEFAULTS="$BUILD_DIR/local.defaults"

DEVICE_DEFAULTS="$FW_DIR/configs/$DEVICE.defaults"


mkdir -p "$BUILD_DIR"


cat > "$LOCAL_DEFAULTS" <<CFG
CONFIG_WS_WIFI_SSID_24="${WIFI_AP_SSID}"
CONFIG_WS_WIFI_PASS_24="${WIFI_AP_PASSWORD}"

CONFIG_WS_WIFI_SSID_5="${WIFI_AP_SSID}"
CONFIG_WS_WIFI_PASS_5="${WIFI_AP_PASSWORD}"

CONFIG_WS_MQTT_URI="mqtt://${AP_HOST}:${MQTT_PORT}"
CONFIG_WS_MQTT_USER="${MQTT_USER}"
CONFIG_WS_MQTT_PASSWORD="${MQTT_PASSWORD}"
CONFIG_WS_MQTT_BASE_TOPIC="${MQTT_BASE_TOPIC}"

CONFIG_WS_OTA_PORT=${OTA_PORT_VALUE}
CONFIG_WS_CSI_UDP_PORT=${CSI_UDP_PORT:-5007}
CONFIG_WS_CSI_PROMISCUOUS=n
CFG


rm -f \
    "$SDKCONFIG_FILE" \
    "$SDKCONFIG_FILE.old"


DEFAULTS="sdkconfig.defaults;configs/${DEVICE}.defaults;${LOCAL_DEFAULTS}"


echo
echo "============================================================"
echo " WIFI SENSING - BUILD"
echo "============================================================"
echo "device          : $DEVICE"
echo "version         : $VERSION"
echo "ssid            : $WIFI_AP_SSID"
echo "mqtt            : mqtt://${AP_HOST}:${MQTT_PORT}"
echo "ota             : http://${AP_HOST}:${OTA_PORT_VALUE}"
echo "build           : $BUILD_DIR"
echo "============================================================"
echo


cd "$FW_DIR"


IDF_TARGET=esp32c5 \
idf.py \
    -B "$BUILD_DIR" \
    -DSDKCONFIG="$SDKCONFIG_FILE" \
    -DSDKCONFIG_DEFAULTS="$DEFAULTS" \
    -DPROJECT_VER="$VERSION" \
    build


BIN="$BUILD_DIR/wifi_sensing_rx.bin"


if [[ ! -f "$BIN" ]]; then
    echo "[ERROR] No se generó $BIN"
    exit 1
fi


ARTIFACT_DIR="$ROOT/ota-artifacts/$DEVICE/$VERSION"

mkdir -p "$ARTIFACT_DIR"
mkdir -p "$ROOT/ota-server"


OTA_FILENAME="wifi_sensing_${DEVICE}_${VERSION}.bin"


cp "$BIN" \
   "$ARTIFACT_DIR/firmware.bin"


cp "$BIN" \
   "$ROOT/ota-server/$OTA_FILENAME"


SHA256="$(sha256sum "$BIN" | awk '{print $1}')"

cat > "$ARTIFACT_DIR/build_metadata.txt" <<META
device=$DEVICE
version=$VERSION
idf_version=$IDF_VERSION
idf_path=$IDF_PATH
sha256=$SHA256
csi_udp_port=${CSI_UDP_PORT:-5007}
ampdu_tx=off
ampdu_rx=off
promiscuous=off
META


echo
echo "============================================================"
echo " BUILD OK"
echo "============================================================"
echo "device   : $DEVICE"
echo "version  : $VERSION"
echo "bin      : $BIN"
echo "OTA file : $OTA_FILENAME"
echo "SHA256   : $SHA256"
echo
echo "Artefacto:"
echo "$ARTIFACT_DIR/firmware.bin"
echo
echo "Servidor OTA:"
echo "$ROOT/ota-server/$OTA_FILENAME"
echo "============================================================"
