#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[ERROR] No existe $ROOT/.venv/bin/python"
  exit 1
fi

.venv/bin/python -m pip install -r web/requirements.txt

echo "Web lista. Ejecuta: ./scripts/run_web.sh"
