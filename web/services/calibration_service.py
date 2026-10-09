from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALIB_ROOT = PROJECT_ROOT / "calibrations"
STATE_PATH = PROJECT_ROOT / "web" / "data" / "calibration_state.json"


class CalibrationService:
    def __init__(self):
        CALIB_ROOT.mkdir(parents=True, exist_ok=True)
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slug(value: str) -> str:
        value = unicodedata.normalize("NFKD", value)
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        value = value.lower().strip()
        value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
        return value[:48] or "baseline"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _profile_dir(self, profile_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_-]{3,100}", profile_id):
            raise ValueError("ID de calibración inválido")
        return CALIB_ROOT / profile_id

    def _meta_path(self, profile_id: str) -> Path:
        return self._profile_dir(profile_id) / "metadata.json"

    def _read_json(self, path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)

    def create_profile(self, *, name: str, band: str, note: str = "") -> dict[str, Any]:
        name = str(name).strip()
        note = str(note).strip()
        if len(name) < 3 or len(name) > 80:
            raise ValueError("El nombre debe tener entre 3 y 80 caracteres")
        if band not in {"2.4g", "5g"}:
            raise ValueError("Banda inválida")
        if len(note) > 300:
            raise ValueError("La nota no puede superar 300 caracteres")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        band_tag = "24g" if band == "2.4g" else "5g"
        profile_id = f"{self._slug(name)}_{band_tag}_{stamp}_{uuid.uuid4().hex[:6]}"
        profile_dir = self._profile_dir(profile_id)
        profile_dir.mkdir(parents=True, exist_ok=False)

        payload = {
            "id": profile_id,
            "name": name,
            "note": note,
            "band": band,
            "status": "pending",
            "created_at": self._now(),
            "updated_at": self._now(),
            "baseline_path": str(profile_dir / "baseline.npy"),
            "receiver_metadata_path": str(profile_dir / "receiver_metadata.json"),
        }
        self._write_json(self._meta_path(profile_id), payload)
        return payload

    def get(self, profile_id: str) -> dict[str, Any]:
        payload = self._read_json(self._meta_path(profile_id), None)
        if not isinstance(payload, dict):
            raise ValueError("Calibración no encontrada")
        payload["active"] = self.is_active(profile_id)
        return payload

    def list_profiles(self) -> list[dict[str, Any]]:
        profiles = []
        for path in CALIB_ROOT.glob("*/metadata.json"):
            payload = self._read_json(path, None)
            if isinstance(payload, dict):
                payload["active"] = self.is_active(str(payload.get("id", "")))
                profiles.append(payload)
        profiles.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        return profiles

    def mark_failed(self, profile_id: str, message: str) -> dict[str, Any]:
        payload = self.get(profile_id)
        payload.pop("active", None)
        payload["status"] = "failed"
        payload["error"] = str(message)
        payload["updated_at"] = self._now()
        self._write_json(self._meta_path(profile_id), payload)
        return self.get(profile_id)

    def finalize(self, profile_id: str) -> dict[str, Any]:
        payload = self.get(profile_id)
        payload.pop("active", None)
        baseline = Path(payload["baseline_path"])
        receiver_meta = Path(payload["receiver_metadata_path"])

        if not baseline.is_file():
            raise RuntimeError("El receiver terminó sin generar baseline.npy")
        technical = self._read_json(receiver_meta, None)
        if not isinstance(technical, dict):
            raise RuntimeError("Falta receiver_metadata.json de la calibración")

        payload.update(technical)
        payload["status"] = "ready"
        payload["updated_at"] = self._now()
        payload["baseline_path"] = str(baseline)
        payload["receiver_metadata_path"] = str(receiver_meta)
        self._write_json(self._meta_path(profile_id), payload)
        return self.get(profile_id)

    def _state(self) -> dict[str, Any]:
        state = self._read_json(STATE_PATH, {})
        return state if isinstance(state, dict) else {}

    def is_active(self, profile_id: str) -> bool:
        state = self._state()
        return profile_id in set(state.values())

    def activate(self, profile_id: str) -> dict[str, Any]:
        profile = self.get(profile_id)
        if profile.get("status") != "ready":
            raise ValueError("Sólo se puede activar un baseline listo")
        if not Path(profile["baseline_path"]).is_file():
            raise ValueError("baseline.npy no existe")

        state = self._state()
        state[profile["band"]] = profile_id
        self._write_json(STATE_PATH, state)
        return self.get(profile_id)

    def use_published(self, band: str) -> dict[str, Any]:
        if band not in {"2.4g", "5g"}:
            raise ValueError("Banda inválida")
        state = self._state()
        state.pop(band, None)
        self._write_json(STATE_PATH, state)
        return {"band": band, "mode": "published", "active": None}

    def active_profile(self, band: str) -> dict[str, Any] | None:
        profile_id = self._state().get(band)
        if not profile_id:
            return None
        try:
            profile = self.get(profile_id)
        except ValueError:
            return None
        if profile.get("status") != "ready":
            return None
        if not Path(profile.get("baseline_path", "")).is_file():
            return None
        return profile

    def active_baseline_path(self, band: str) -> Path | None:
        profile = self.active_profile(band)
        return Path(profile["baseline_path"]) if profile else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "profiles": self.list_profiles(),
            "active": {
                band: self.active_profile(band)
                for band in ("2.4g", "5g")
            },
        }
