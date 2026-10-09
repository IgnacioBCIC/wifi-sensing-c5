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
    echo "  $0 rx02 1.0.3"
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


if [[ ! -f "$ENV_FILE" ]]; then
    echo "[ERROR] No existe $ENV_FILE"
    exit 1
fi


set -a
source "$ENV_FILE"
set +a


DEVICE_UPPER="${DEVICE^^}"

SERIAL_VAR="${DEVICE_UPPER}_SERIAL"
MAC_VAR="${DEVICE_UPPER}_BASE_MAC"

SERIAL="${!SERIAL_VAR:-}"
EXPECTED_MAC="${!MAC_VAR:-}"


if [[ -z "$SERIAL" ]]; then
    echo "[ERROR] $SERIAL_VAR no definido en .env"
    exit 1
fi


if [[ -z "$EXPECTED_MAC" ]]; then
    echo "[ERROR] $MAC_VAR no definido en .env"
    exit 1
fi


EXPECTED_MAC="$(
    printf '%s' "$EXPECTED_MAC" |
    tr '[:upper:]' '[:lower:]'
)"


if [[ ! -e "$SERIAL" ]]; then
    echo "[ERROR] Puerto no encontrado:"
    echo "        $SERIAL"
    exit 1
fi


WS_IDF_PATH="${WS_IDF_PATH:-$HOME/esp/esp-idf-v6.1}"

if [[ ! -f "$WS_IDF_PATH/export.sh" ]]; then
    echo "[ERROR] No existe ESP-IDF 6.1 en: $WS_IDF_PATH"
    exit 1
fi

source "$WS_IDF_PATH/export.sh" >/dev/null

IDF_VERSION="$(idf.py --version 2>&1 | head -n 1)"

if [[ "$IDF_VERSION" != *"v6.1"* && "$IDF_VERSION" != *"6.1."* ]]; then
    echo "[ERROR] Esta V2 requiere ESP-IDF 6.1.x"
    echo "        detectado: $IDF_VERSION"
    exit 1
fi


echo
echo "======================================"
echo " WIFI SENSING - PROVISIONING"
echo "======================================"
echo "device esperado : $DEVICE"
echo "version         : $VERSION"
echo "serial          : $SERIAL"
echo "MAC esperada    : $EXPECTED_MAC"
echo "======================================"
echo


if python -m esptool --help 2>&1 |
   grep -q 'read-mac'; then

    READ_MAC_CMD="read-mac"

else

    READ_MAC_CMD="read_mac"

fi


MAC_OUTPUT="$(
    python -m esptool \
        --chip esp32c5 \
        -p "$SERIAL" \
        "$READ_MAC_CMD" \
        2>&1
)"


printf '%s\n' "$MAC_OUTPUT"


DETECTED_MAC="$(
    printf '%s\n' "$MAC_OUTPUT" |
    grep -Eio \
        '([0-9a-f]{2}:){5}[0-9a-f]{2}' |
    tail -n 1 |
    tr '[:upper:]' '[:lower:]'
)"


if [[ -z "$DETECTED_MAC" ]]; then
    echo
    echo "[ERROR] No se pudo determinar la MAC física."
    exit 1
fi


echo
echo "MAC detectada : $DETECTED_MAC"
echo "MAC esperada  : $EXPECTED_MAC"


if [[ "$DETECTED_MAC" != "$EXPECTED_MAC" ]]; then
    echo
    echo "======================================"
    echo " ABORTADO - IDENTIDAD INCORRECTA"
    echo "======================================"
    echo "Se solicitó : $DEVICE"
    echo "Esperada    : $EXPECTED_MAC"
    echo "Detectada   : $DETECTED_MAC"
    echo
    echo "NO se modificó la flash."
    exit 1
fi


echo
echo "[OK] Identidad física validada."


"$SCRIPT_DIR/build_rx.sh" \
    "$DEVICE" \
    "$VERSION"


BUILD_DIR="$FW_DIR/build/$DEVICE"


echo
echo "======================================"
echo " BORRADO COMPLETO"
echo "======================================"
echo


cd "$FW_DIR"


idf.py \
    -B "$BUILD_DIR" \
    -p "$SERIAL" \
    erase-flash


echo
echo "======================================"
echo " FLASH FIRMWARE"
echo "======================================"
echo


idf.py \
    -B "$BUILD_DIR" \
    -p "$SERIAL" \
    flash


echo
echo "======================================"
echo " PROVISIONING COMPLETADO"
echo "======================================"
echo "device  : $DEVICE"
echo "version : $VERSION"
echo "MAC     : $DETECTED_MAC"
echo
echo "A partir de ahora las actualizaciones"
echo "normales deben realizarse mediante OTA."
echo "======================================"
