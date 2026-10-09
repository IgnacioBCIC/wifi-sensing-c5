#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage()
{
    echo "Uso:"
    echo "  $0 <2.4g|5g> <rx01|rx02|rx03|all>"
    echo
    echo "Ejemplos:"
    echo "  $0 5g rx01"
    echo "  $0 5g all"
    echo "  $0 2.4g all"
}

if [[ $# -ne 2 ]]; then
    usage
    exit 1
fi

BAND="$1"
DEVICES="$2"

case "$BAND" in
    2.4|2.4g) BAND="2.4g" ;;
    5|5g)     BAND="5g" ;;
    *)
        echo "[ERROR] Banda inválida: $BAND"
        usage
        exit 1
        ;;
esac

cd "$ROOT"

if [[ ! -f ".env" ]]; then
    echo "[ERROR] No existe $ROOT/.env"
    exit 1
fi

set -a
source .env
set +a

if [[ "$DEVICES" == "all" ]]; then
    DEVICES="$RX_DEVICES"
fi

PY="$ROOT/.venv/bin/python"
CSI_PORT="${CSI_UDP_PORT:-5007}"

if [[ ! -x "$PY" ]]; then
    echo "[ERROR] No existe $PY"
    exit 1
fi

echo
echo "======================================"
echo " WIFI SENSING V2 - EXPERIMENTO"
echo "======================================"
echo "Banda      : $BAND"
echo "Receptores : $DEVICES"
echo "CSI UDP    : $CSI_PORT"
echo "======================================"
echo

cleanup()
{
    if [[ -n "${RECEIVER_PID:-}" ]] && kill -0 "$RECEIVER_PID" 2>/dev/null; then
        kill -TERM "$RECEIVER_PID" 2>/dev/null || true

        for _ in $(seq 1 20); do
            if ! kill -0 "$RECEIVER_PID" 2>/dev/null; then
                break
            fi
            sleep 0.1
        done

        if kill -0 "$RECEIVER_PID" 2>/dev/null; then
            kill -KILL "$RECEIVER_PID" 2>/dev/null || true
        fi

        wait "$RECEIVER_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT
trap 'exit 130' INT TERM

"$PY" "$ROOT/controller/csi_receiver.py" &
RECEIVER_PID=$!

RECEIVER_READY=0
for _ in $(seq 1 100); do
    if ! kill -0 "$RECEIVER_PID" 2>/dev/null; then
        echo "[ERROR] csi_receiver.py terminó al iniciar."
        wait "$RECEIVER_PID" || true
        exit 1
    fi

    if ss -H -lun 2>/dev/null | grep -Eq ":${CSI_PORT}[[:space:]]"; then
        RECEIVER_READY=1
        break
    fi

    sleep 0.1
done

if [[ "$RECEIVER_READY" -ne 1 ]]; then
    echo "[ERROR] csi_receiver.py no abrió UDP :$CSI_PORT en 10 s."
    exit 1
fi

sleep 0.5

echo "[OK] csi_receiver listo en UDP :$CSI_PORT"

"$PY" \
    "$ROOT/controller/controller.py" \
    --band "$BAND" \
    --devices "$DEVICES"
