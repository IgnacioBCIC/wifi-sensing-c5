#!/usr/bin/env bash
set -euo pipefail

BAND="${1:-}"

PROFILE_24="wifi-sensing-24"
PROFILE_5="wifi-sensing-5"
IFACE="wlo1"

case "$BAND" in
    2.4g|2.4|24g|24)
        TARGET="$PROFILE_24"
        OTHER="$PROFILE_5"
        EXPECTED_CHANNEL="6"
        ;;
    5g|5)
        TARGET="$PROFILE_5"
        OTHER="$PROFILE_24"
        EXPECTED_CHANNEL="48"
        ;;
    *)
        echo "Uso:"
        echo "  $0 2.4g"
        echo "  $0 5g"
        exit 1
        ;;
esac

echo "========================================"
echo " WIFI SENSING - CAMBIO DE BANDA"
echo "========================================"
echo "Banda solicitada : $BAND"
echo "Perfil objetivo  : $TARGET"
echo "Interfaz         : $IFACE"
echo "Canal esperado   : $EXPECTED_CHANNEL"
echo "========================================"

echo
echo "[1/3] Desactivando perfil anterior..."
sudo -n /usr/bin/nmcli connection down "$OTHER" >/dev/null 2>&1 || true

echo "[2/3] Activando $TARGET..."
sudo -n /usr/bin/nmcli connection up "$TARGET"

sleep 2

echo
echo "[3/3] Verificando..."
ACTIVE="$(
    /usr/bin/nmcli -t -f NAME,TYPE connection show --active |
    grep '^wifi-sensing-' || true
)"

echo
echo "Conexiones Wi-Fi Sensing activas:"
echo "${ACTIVE:-ninguna}"

CURRENT="$(
    /usr/bin/nmcli -t -f GENERAL.CONNECTION device show "$IFACE" |
    cut -d: -f2-
)"

if [[ "$CURRENT" != "$TARGET" ]]; then
    echo
    echo "[ERROR] Perfil activo inesperado:"
    echo "  esperado : $TARGET"
    echo "  actual   : $CURRENT"
    exit 1
fi

echo
echo "========================================"
echo "[OK] Banda $BAND activa"
echo "[OK] Perfil: $CURRENT"
echo "========================================"
