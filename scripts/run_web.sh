\
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[ERROR] No existe .venv/bin/python"
  exit 1
fi

if ! .venv/bin/python -c 'import flask' >/dev/null 2>&1; then
  echo "[ERROR] Flask no está instalado."
  echo "Ejecuta primero:"
  echo "  ./scripts/setup_web.sh"
  exit 1
fi

echo
echo "============================================================"
echo " WIFI SENSING WEB"
echo "============================================================"
echo "Local    : http://127.0.0.1:5000"
echo "AP típico: http://10.42.0.1:5000"
echo "============================================================"
echo

exec .venv/bin/python web/app.py
