from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "controller" / "experiment.json"
BACKUP_DIR = PROJECT_ROOT / "web" / "data" / "config-backups"


EDITABLE_FIELDS = {
    "probe.rate_hz": {
        "label": "Tasa de captura (Hz)",
        "type": "int",
        "min": 20,
        "max": 500,
        "help": "200 Hz es la configuración validada actual.",
    },
    "capture.blocks": {
        "label": "Bloques por experimento",
        "type": "int",
        "min": 1,
        "max": 20,
        "help": "Actualmente se usan 4 bloques.",
    },
    "capture.target_packets": {
        "label": "CSI objetivo por bloque",
        "type": "int",
        "min": 40,
        "max": 4000,
        "help": "400 CSI @ 200 Hz equivalen a ~2 segundos.",
    },
    "capture.max_capture_ms": {
        "label": "Timeout máximo por bloque (ms)",
        "type": "int",
        "min": 500,
        "max": 30000,
        "help": "Debe ser mayor a la duración nominal calculada.",
    },
    "capture.settle_time_ms": {
        "label": "Estabilización previa (ms)",
        "type": "int",
        "min": 0,
        "max": 10000,
        "help": "Espera antes de iniciar la captura formal.",
    },
    "healthcheck.enabled": {
        "label": "Healthcheck habilitado",
        "type": "bool",
        "help": "Mantener habilitado para experimentos formales.",
    },
    "healthcheck.search_target_packets": {
        "label": "CSI por healthcheck",
        "type": "int",
        "min": 20,
        "max": 1000,
        "help": "Actualmente 100 muestras por RX.",
    },
    "healthcheck.rate_hz": {
        "label": "Tasa del healthcheck (Hz)",
        "type": "int",
        "min": 20,
        "max": 500,
        "help": "Actualmente 200 Hz.",
    },
    "healthcheck.good_unique_ratio": {
        "label": "Ratio mínimo de CSI único",
        "type": "float",
        "min": 0.50,
        "max": 1.00,
        "step": 0.01,
        "help": "0.95 = 95 %. Umbral para LOCKED_GOOD.",
    },
    "healthcheck.required_consecutive_passes": {
        "label": "Checks buenos consecutivos",
        "type": "int",
        "min": 1,
        "max": 5,
        "help": "Actualmente basta 1.",
    },
    "healthcheck.max_soft_reinits_per_boot": {
        "label": "Máximo soft-reinit por boot",
        "type": "int",
        "min": 1,
        "max": 10,
        "help": "Recuperación selectiva CSI.",
    },
    "healthcheck.max_full_restarts": {
        "label": "Máximo de reinicios completos",
        "type": "int",
        "min": 1,
        "max": 5,
        "help": "Sólo si el soft-reinit no recupera CSI.",
    },
    "block_quality.max_attempts": {
        "label": "Intentos máximos por bloque",
        "type": "int",
        "min": 1,
        "max": 5,
        "help": "Un bloque inválido puede repetirse.",
    },
    "block_quality.retry_settle_time_ms": {
        "label": "Espera antes de repetir bloque (ms)",
        "type": "int",
        "min": 0,
        "max": 5000,
        "help": "Pausa entre intentos del mismo bloque.",
    },
    "recovery.restart_enabled": {
        "label": "Recuperación con restart habilitada",
        "type": "bool",
        "help": "Permite reiniciar RX si CSI_FROZEN persiste.",
    },
}


def _get_path(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        current = current[part]
    return current


def _set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    current = data
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value


class ExperimentConfigService:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def editable_snapshot(self) -> dict[str, Any]:
        cfg = self.load()
        values = {
            key: _get_path(cfg, key)
            for key in EDITABLE_FIELDS
        }
        return {
            "values": values,
            "fields": EDITABLE_FIELDS,
            "raw": cfg,
            "derived": self.derived(cfg),
        }

    @staticmethod
    def derived(cfg: dict[str, Any]) -> dict[str, Any]:
        rate = float(cfg["probe"]["rate_hz"])
        packets = int(cfg["capture"]["target_packets"])
        nominal_ms = int(round((packets / rate) * 1000)) if rate > 0 else None
        espectre_ok = int(rate) == 200 and packets == 400
        return {
            "nominal_duration_ms": nominal_ms,
            "espectre_zero_shot_a_compatible": espectre_ok,
            "espectre_requirement": "200 Hz + 400 CSI/bloque",
        }

    def _coerce(self, key: str, value: Any) -> Any:
        spec = EDITABLE_FIELDS[key]
        kind = spec["type"]

        if kind == "bool":
            if isinstance(value, bool):
                return value
            if str(value).lower() in {"true", "1", "yes", "on"}:
                return True
            if str(value).lower() in {"false", "0", "no", "off"}:
                return False
            raise ValueError(f"{key}: booleano inválido")

        if kind == "int":
            converted = int(value)
        elif kind == "float":
            converted = float(value)
        else:
            raise ValueError(f"{key}: tipo no soportado")

        if "min" in spec and converted < spec["min"]:
            raise ValueError(f"{key}: mínimo {spec['min']}")
        if "max" in spec and converted > spec["max"]:
            raise ValueError(f"{key}: máximo {spec['max']}")
        return converted

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        cfg = self.load()
        new_cfg = copy.deepcopy(cfg)

        unknown = set(values) - set(EDITABLE_FIELDS)
        if unknown:
            raise ValueError(
                "Campos no editables: " + ", ".join(sorted(unknown))
            )

        for key, raw_value in values.items():
            _set_path(new_cfg, key, self._coerce(key, raw_value))

        rate = float(new_cfg["probe"]["rate_hz"])
        packets = int(new_cfg["capture"]["target_packets"])
        nominal_ms = int(round((packets / rate) * 1000))
        new_cfg["capture"]["nominal_duration_ms"] = nominal_ms

        min_capture_ms = nominal_ms + 200
        if int(new_cfg["capture"]["max_capture_ms"]) < min_capture_ms:
            new_cfg["capture"]["max_capture_ms"] = min_capture_ms

        health_rate = float(new_cfg["healthcheck"]["rate_hz"])
        health_packets = int(new_cfg["healthcheck"]["search_target_packets"])
        health_nominal = int(round((health_packets / health_rate) * 1000))
        if int(new_cfg["healthcheck"]["max_capture_ms"]) < health_nominal + 250:
            new_cfg["healthcheck"]["max_capture_ms"] = health_nominal + 250

        min_frames = int(new_cfg["healthcheck"].get("min_frames", 20))
        if health_packets < min_frames:
            raise ValueError(
                "healthcheck.search_target_packets no puede ser menor "
                f"que healthcheck.min_frames ({min_frames})"
            )

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"experiment_{stamp}.json"
        backup.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        self.path.write_text(
            json.dumps(new_cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return self.editable_snapshot()
