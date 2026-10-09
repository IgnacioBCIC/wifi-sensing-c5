

from __future__ import annotations

import os
import re
import json
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config_service import ExperimentConfigService
from .calibration_service import CalibrationService
from .database import Database
from .mqtt_state import MqttState


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
LOG_DIR = PROJECT_ROOT / "test-logs" / "web"
AE_MODEL_DIRS = {
    "2.4g": PROJECT_ROOT / "models" / "ae_presence" / "ae_24g_hybrid_v1",
    "5g": PROJECT_ROOT / "models" / "ae_presence" / "ae_5g_hybrid_v1",
}
AE_CALIBRATOR = PROJECT_ROOT / "scripts" / "ae_calibrate_hybrid.py"


class JobManager:
    def __init__(
        self,
        mqtt_state: MqttState,
        database: Database,
        config_service: ExperimentConfigService,
    ):
        self.mqtt = mqtt_state
        self.db = database
        self.config = config_service
        self.calibrations = CalibrationService()
        self.lock = threading.RLock()
        self.current: dict[str, Any] | None = None
        self.controller_proc: subprocess.Popen | None = None
        self.receiver_proc: subprocess.Popen | None = None
        self.live_stop_file: Path | None = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (PROJECT_ROOT / "web" / "data" / "live-stop").mkdir(
            parents=True, exist_ok=True
        )

    def get_current(self) -> dict[str, Any] | None:
        with self.lock:
            if not self.current:
                return None
            job = dict(self.current)


        if job.get("state") in {"queued", "countdown", "starting", "running", "stopping"}:
            events = self.mqtt.events_since(job["started_at"])
            job["summary"] = {
                "condition": job.get("condition"),
                "baseline": {
                    "id": job.get("baseline_id"),
                    "name": job.get("baseline_name"),
                    "mode": job.get("baseline_mode"),
                },
                "health": events.get("health", []),
                "model": events.get("model", []),
            }
        return job

    def is_busy(self) -> bool:
        with self.lock:
            return bool(
                self.current
                and self.current["state"]
                in {"queued", "countdown", "starting", "running", "stopping"}
            )

    def _new_job(
        self,
        *,
        kind: str,
        band: str,
        devices: list[str],
        condition: str | None,
        model_mode: str | None,
        countdown_s: int,
    ) -> dict[str, Any]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_id = f"{kind}_{stamp}_{uuid.uuid4().hex[:6]}"
        return {
            "id": job_id,
            "kind": kind,
            "band": band,
            "devices": devices,
            "condition": condition,
            "model_mode": model_mode,
            "countdown_s": countdown_s,
            "state": "queued",
            "started_at": time.time(),
            "ended_at": None,
            "return_code": None,
            "progress": {"block": 0, "blocks": 0, "health_check": 0},
            "message": "En cola",
            "summary": {},
            "controller_log": str(LOG_DIR / f"{job_id}_controller.txt"),
            "receiver_log": str(LOG_DIR / f"{job_id}_receiver.txt"),
        }

    def _ae_model_info(self, band: str) -> dict[str, Any]:
        model_dir = AE_MODEL_DIRS.get(band)
        if model_dir is None:
            raise ValueError("Banda AE inválida")
        metadata_path = model_dir / "metadata.json"
        if not metadata_path.is_file():
            raise ValueError(f"No existe modelo AE híbrido para {band}: {model_dir}")
        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"metadata AE inválido: {exc}") from exc
        if meta.get("model_type") != "hybrid_dual_branch_v1":
            raise ValueError("El modelo AE no es hybrid_dual_branch_v1")
        if meta.get("band") and meta.get("band") != band:
            raise ValueError("El modelo AE pertenece a otra banda")
        return {
            "band": band,
            "model_id": meta.get("model_id", model_dir.name),
            "model_dir": str(model_dir),
            "ready": True,
            "branches": meta.get("branches", {}),
            "calibration": meta.get("calibration"),
        }

    def ae_models_snapshot(self) -> dict[str, Any]:
        models = {}
        for band in ("2.4g", "5g"):
            try:
                models[band] = self._ae_model_info(band)
            except ValueError as exc:
                models[band] = {
                    "band": band,
                    "ready": False,
                    "error": str(exc),
                    "model_dir": str(AE_MODEL_DIRS[band]),
                }
        return {"models": models}

    def _validate_model_mode(
        self, *, band: str, devices: list[str], model_mode: str
    ) -> dict[str, Any]:
        if model_mode in {"zero_shot_a", "ae_hybrid_v1"} and devices != [
            "rx01", "rx02", "rx03"
        ]:
            raise ValueError("El modelo requiere rx01, rx02 y rx03 en ese orden.")

        if model_mode == "zero_shot_a":
            derived = self.config.derived(self.config.load())
            if not derived["espectre_zero_shot_a_compatible"]:
                raise ValueError(
                    "Zero-shot A requiere exactamente 200 Hz + 400 CSI/bloque."
                )
            return {}

        if model_mode == "ae_hybrid_v1":
            derived = self.config.derived(self.config.load())
            if not derived["espectre_zero_shot_a_compatible"]:
                raise ValueError("AE híbrido requiere 200 Hz + 400 CSI/bloque.")
            info = self._ae_model_info(band)
            return {
                "model_id": info["model_id"],
                "model_dir": info["model_dir"],
            }

        return {}

    def start_ae_calibration(
        self, *, band: str, countdown_s: int = 5, target_blocks: int = 20
    ) -> dict[str, Any]:
        if self.is_busy():
            raise RuntimeError("Ya existe un trabajo activo.")
        if target_blocks < 15 or target_blocks > 120:
            raise ValueError("target_blocks debe estar entre 15 y 120")

        model = self._validate_model_mode(
            band=band,
            devices=["rx01", "rx02", "rx03"],
            model_mode="ae_hybrid_v1",
        )
        job = self._new_job(
            kind="ae_calibration",
            band=band,
            devices=["rx01", "rx02", "rx03"],
            condition="empty",
            model_mode="ae_hybrid_v1",
            countdown_s=countdown_s,
        )
        job.update(model)
        job.update(target_blocks=target_blocks)
        job["progress"] = {
            "block": 0, "blocks": target_blocks, "health_check": 0
        }
        return self._start_prepared(job)

    def start_health(self, *, band: str, devices: list[str]) -> dict[str, Any]:
        return self._start(
            kind="health",
            band=band,
            devices=devices,
            condition=None,
            model_mode=None,
            countdown_s=0,
        )

    def start_experiment(
        self,
        *,
        band: str,
        devices: list[str],
        baseline_id: str,
        model_mode: str,
        countdown_s: int,
    ) -> dict[str, Any]:
        model_extra = self._validate_model_mode(
            band=band, devices=devices, model_mode=model_mode
        )
        job = self._new_job(
            kind="experiment",
            band=band,
            devices=devices,
            condition=None,
            model_mode=model_mode,
            countdown_s=countdown_s,
        )
        job.update(model_extra)
        job.update(
            self._resolve_baseline_selection(
                band=band,
                baseline_id=baseline_id,
                model_mode=model_mode,
            )
        )
        return self._start_prepared(job)

    def start_live(
        self,
        *,
        band: str,
        devices: list[str],
        baseline_id: str,
        model_mode: str,
        countdown_s: int,
    ) -> dict[str, Any]:
        model_extra = self._validate_model_mode(
            band=band, devices=devices, model_mode=model_mode
        )
        job = self._new_job(
            kind="live",
            band=band,
            devices=devices,
            condition=None,
            model_mode=model_mode,
            countdown_s=countdown_s,
        )
        job.update(model_extra)
        job.update(
            self._resolve_baseline_selection(
                band=band,
                baseline_id=baseline_id,
                model_mode=model_mode,
            )
        )
        return self._start_prepared(job)

    def _resolve_baseline_selection(
        self,
        *,
        band: str,
        baseline_id: str,
        model_mode: str,
    ) -> dict[str, Any]:
        if model_mode != "zero_shot_a":
            return {
                "baseline_id": None,
                "baseline_name": None,
                "baseline_path": None,
                "baseline_mode": "none",
            }

        baseline_id = str(baseline_id or "published").strip()

        if baseline_id == "published":
            return {
                "baseline_id": "published",
                "baseline_name": "Original ESPectre",
                "baseline_path": None,
                "baseline_mode": "published",
            }

        profile = self.calibrations.get(baseline_id)

        if profile.get("status") != "ready":
            raise ValueError("El baseline local seleccionado no está listo.")

        if profile.get("band") != band:
            raise ValueError(
                "El baseline seleccionado pertenece a otra banda."
            )

        baseline_path = Path(str(profile.get("baseline_path", "")))
        if not baseline_path.is_file():
            raise ValueError(
                "No existe baseline.npy del perfil seleccionado."
            )

        return {
            "baseline_id": profile["id"],
            "baseline_name": profile["name"],
            "baseline_path": str(baseline_path),
            "baseline_mode": "local",
        }

    def start_calibration(
        self,
        *,
        name: str,
        band: str,
        note: str,
        countdown_s: int = 5,
    ) -> dict[str, Any]:
        if self.is_busy():
            raise RuntimeError("Ya existe un trabajo activo.")

        derived = self.config.derived(self.config.load())
        if not derived["espectre_zero_shot_a_compatible"]:
            raise ValueError(
                "Calibración requiere exactamente 200 Hz + 400 CSI/bloque."
            )

        profile = self.calibrations.create_profile(
            name=name,
            band=band,
            note=note,
        )
        job = self._new_job(
            kind="calibration",
            band=band,
            devices=["rx01", "rx02", "rx03"],
            condition="empty",
            model_mode=None,
            countdown_s=countdown_s,
        )
        job.update(
            calibration_id=profile["id"],
            calibration_name=profile["name"],
            calibration_output=profile["baseline_path"],
            target_blocks=13,
        )
        return self._start_prepared(job)

    def start_calibration_validation(
        self,
        *,
        profile_id: str,
        condition: str,
        countdown_s: int = 5,
    ) -> dict[str, Any]:
        if condition not in {"empty", "presence_static"}:
            raise ValueError("Condición de validación inválida")
        profile = self.calibrations.get(profile_id)
        if profile.get("status") != "ready":
            raise ValueError("El baseline seleccionado no está listo")

        job = self._new_job(
            kind="calibration_validation",
            band=profile["band"],
            devices=["rx01", "rx02", "rx03"],
            condition=condition,
            model_mode="zero_shot_a",
            countdown_s=countdown_s,
        )
        job.update(
            calibration_id=profile_id,
            baseline_id=profile["id"],
            baseline_name=profile["name"],
            baseline_path=profile["baseline_path"],
            baseline_mode="local",
            target_blocks=6,
        )
        return self._start_prepared(job)

    def _start_prepared(self, job: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.is_busy():
                raise RuntimeError("Ya existe un trabajo activo.")
            self.current = job
            self.db.upsert_job(job)

        thread = threading.Thread(
            target=self._worker,
            args=(job["id"],),
            daemon=True,
        )
        thread.start()
        return dict(job)

    def _start(self, **kwargs) -> dict[str, Any]:
        return self._start_prepared(self._new_job(**kwargs))

    def _set(self, job_id: str, **changes):
        with self.lock:
            if not self.current or self.current["id"] != job_id:
                return
            self.current.update(changes)
            self.db.upsert_job(self.current)

    def _update_progress(self, job_id: str, **changes):
        with self.lock:
            if not self.current or self.current["id"] != job_id:
                return
            progress = dict(self.current.get("progress", {}))
            progress.update(changes)
            self.current["progress"] = progress

    @staticmethod
    def _terminate(proc: subprocess.Popen | None):
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=4)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass

    def cancel(self) -> dict[str, Any]:
        with self.lock:
            if not self.current or not self.is_busy():
                raise RuntimeError("No hay trabajo activo.")
            job_id = self.current["id"]
            kind = self.current.get("kind")
            state = self.current.get("state")
            self.current["state"] = "stopping"

            if kind in {"live", "ae_calibration"} and state == "running" and self.live_stop_file:
                self.current["message"] = (
                    "Detención solicitada; finalizando el bloque actual..."
                )
                self.live_stop_file.parent.mkdir(parents=True, exist_ok=True)
                self.live_stop_file.touch()
                self.db.upsert_job(self.current)
                return self.get_current() or {}

            self.current["message"] = "Cancelando..."

        self._terminate(self.controller_proc)
        self._terminate(self.receiver_proc)
        self._set(
            job_id,
            state="cancelled",
            message="Cancelado por el operador",
            ended_at=time.time(),
        )
        return self.get_current() or {}

    def stop_all(self):
        self._terminate(self.controller_proc)
        self._terminate(self.receiver_proc)

    def _pipe_output(
        self,
        job_id: str,
        proc: subprocess.Popen,
        log_path: Path,
        source: str,
    ):
        with log_path.open("a", encoding="utf-8") as log:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                log.write(line)
                log.flush()

                if source == "controller":
                    block = re.search(r"BLOQUE\s+(\d+)/(\d+|∞|LIVE)", line)
                    if block:
                        total_token = block.group(2)
                        self._update_progress(
                            job_id,
                            block=int(block.group(1)),
                            blocks=(
                                int(total_token)
                                if total_token.isdigit()
                                else 0
                            ),
                        )

                    health = re.search(r"\[HEALTH-FAST\]\s+check=(\d+)", line)
                    if health:
                        self._update_progress(
                            job_id,
                            health_check=int(health.group(1)),
                        )

                    if "INICIO DE CAPTURA AUTOMATICA" in line:
                        self._set(job_id, message="Capturando bloques CSI")
                    if "INICIO DE CAPTURA EN VIVO" in line:
                        self._set(job_id, message="Captura en vivo activa")
                    if "[LIVE-WARN] block=" in line and "omitido:" in line:
                        self._set(
                            job_id,
                            message="Bloque live omitido; recuperando...",
                        )
                    if "[LIVE-RECOVERY]" in line:
                        self._set(
                            job_id,
                            message="Resincronizando RX para continuar live",
                        )

    def _spawn(
        self,
        command: list[str],
        log_path: Path,
        job_id: str,
        source: str,
    ) -> subprocess.Popen:
        proc = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        threading.Thread(
            target=self._pipe_output,
            args=(job_id, proc, log_path, source),
            daemon=True,
        ).start()
        return proc

    def _worker(self, job_id: str):
        with self.lock:
            if not self.current or self.current["id"] != job_id:
                return
            job = dict(self.current)

        try:
            countdown = int(job.get("countdown_s", 0))
            if countdown > 0:
                self._set(
                    job_id,
                    state="countdown",
                    message=f"Inicio en {countdown} s",
                )
                for remaining in range(countdown, 0, -1):
                    with self.lock:
                        if not self.current or self.current["state"] == "cancelled":
                            return
                    self._set(job_id, message=f"Inicio en {remaining} s")
                    time.sleep(1)

            self._set(job_id, state="starting", message="Iniciando CSI receiver")

            receiver_cmd = [
                str(PYTHON),
                str(PROJECT_ROOT / "controller" / "csi_receiver.py"),
            ]

            if job.get("model_mode") == "zero_shot_a":
                receiver_cmd += [
                    "--espectre-model-dir",
                    "models/espectre-sense",
                    "--espectre-devices",
                    "rx01,rx02,rx03",
                ]
                baseline_path = job.get("baseline_path")
                if baseline_path:
                    receiver_cmd += [
                        "--espectre-baseline",
                        str(baseline_path),
                    ]

            if job.get("model_mode") == "ae_hybrid_v1":
                receiver_cmd += [
                    "--ae-model-dir",
                    str(job["model_dir"]),
                    "--ae-devices",
                    "rx01,rx02,rx03",
                ]

            if job.get("kind") == "calibration":
                receiver_cmd += [
                    "--calibration-output",
                    str(job["calibration_output"]),
                    "--calibration-name",
                    str(job["calibration_name"]),
                    "--calibration-target-blocks",
                    str(job["target_blocks"]),
                    "--espectre-devices",
                    "rx01,rx02,rx03",
                ]

            self.receiver_proc = self._spawn(
                receiver_cmd,
                Path(job["receiver_log"]),
                job_id,
                "receiver",
            )


            time.sleep(1.0)
            if self.receiver_proc.poll() is not None:
                raise RuntimeError(
                    "csi_receiver terminó antes de iniciar el controller. "
                    "Revisa el log del receiver."
                )

            if job["kind"] in {"calibration", "calibration_validation"}:
                controller_cmd = [
                    str(PYTHON),
                    str(PROJECT_ROOT / "controller" / "calibration_capture.py"),
                    "--band",
                    job["band"],
                    "--devices",
                    ",".join(job["devices"]),
                    "--blocks",
                    str(job["target_blocks"]),
                ]
            else:
                controller_cmd = [
                    str(PYTHON),
                    str(PROJECT_ROOT / "controller" / "controller.py"),
                    "--band",
                    job["band"],
                    "--devices",
                    ",".join(job["devices"]),
                ]
            if job["kind"] == "health":
                controller_cmd.append("--health-only")
            elif job["kind"] in {"live", "ae_calibration"}:
                stop_dir = PROJECT_ROOT / "web" / "data" / "live-stop"
                self.live_stop_file = stop_dir / f"{job_id}.stop"
                self.live_stop_file.unlink(missing_ok=True)
                controller_cmd += [
                    "--live",
                    "--live-stop-file",
                    str(self.live_stop_file),
                ]

            self._set(
                job_id,
                state="running",
                message=(
                    "Capturando EMPTY para calibración AE"
                    if job["kind"] == "ae_calibration"
                    else "Preparando captura en vivo"
                    if job["kind"] == "live"
                    else "Ejecutando"
                ),
            )
            self.controller_proc = self._spawn(
                controller_cmd,
                Path(job["controller_log"]),
                job_id,
                "controller",
            )

            if job["kind"] == "ae_calibration":
                seen_blocks: set[int] = set()
                target_blocks = int(job["target_blocks"])
                while self.controller_proc.poll() is None:
                    events_now = self.mqtt.events_since(job["started_at"])
                    for event in events_now.get("model", []):
                        if event.get("_model_family") != "ae_hybrid_v1":
                            continue
                        block_id = event.get("block_id")
                        if block_id is not None:
                            seen_blocks.add(int(block_id))
                    count = len(seen_blocks)
                    self._set(
                        job_id,
                        progress={
                            "block": min(count, target_blocks),
                            "blocks": target_blocks,
                            "health_check": 0,
                        },
                        message=(
                            f"Calibración AE EMPTY: "
                            f"{min(count, target_blocks)}/{target_blocks} bloques"
                        ),
                    )
                    if count >= target_blocks:
                        if self.live_stop_file:
                            self.live_stop_file.touch(exist_ok=True)
                        break
                    time.sleep(0.5)

            rc = self.controller_proc.wait()
            self._terminate(self.receiver_proc)
            time.sleep(0.2)

            events = self.mqtt.events_since(job["started_at"])
            summary = {
                "condition": job.get("condition"),
                "baseline": {
                    "id": job.get("baseline_id"),
                    "name": job.get("baseline_name"),
                    "mode": job.get("baseline_mode"),
                },
                "health": events["health"],
                "model": events["model"],
            }

            if rc == 0:
                state = "completed"
                if job["kind"] == "calibration":
                    profile = self.calibrations.finalize(job["calibration_id"])
                    summary["calibration"] = profile
                    message = "Baseline creado y guardado"
                elif job["kind"] == "calibration_validation":
                    message = "Validación de baseline completada"
                elif job["kind"] == "health":
                    message = "Healthcheck completado"
                elif job["kind"] == "ae_calibration":
                    if not AE_CALIBRATOR.is_file():
                        raise RuntimeError(f"No existe calibrador AE: {AE_CALIBRATOR}")
                    calibrate = subprocess.run(
                        [
                            str(PYTHON),
                            str(AE_CALIBRATOR),
                            "--model",
                            str(job["model_dir"]),
                            "--empty-log",
                            str(job["receiver_log"]),
                        ],
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                    )
                    summary["ae_calibration_stdout"] = calibrate.stdout[-12000:]
                    if calibrate.returncode != 0:
                        raise RuntimeError(
                            "Falló calibración AE: "
                            + (
                                calibrate.stderr.strip()
                                or calibrate.stdout.strip()
                            )[-2000:]
                        )
                    report_path = (
                        Path(job["model_dir"]) / "calibration_report.json"
                    )
                    if report_path.is_file():
                        summary["ae_calibration"] = json.loads(
                            report_path.read_text(encoding="utf-8")
                        )
                    message = "AE híbrido calibrado con EMPTY"
                elif job["kind"] == "live":
                    message = "Captura en vivo finalizada"
                else:
                    message = "Experimento completado"
            else:
                state = "failed"
                message = f"Controller terminó con código {rc}"

            self._set(
                job_id,
                state=state,
                message=message,
                ended_at=time.time(),
                return_code=rc,
                summary=summary,
            )
            if self.live_stop_file:
                self.live_stop_file.unlink(missing_ok=True)
                self.live_stop_file = None

        except Exception as exc:
            if job.get("kind") == "calibration" and job.get("calibration_id"):
                try:
                    self.calibrations.mark_failed(job["calibration_id"], str(exc))
                except Exception:
                    pass
            self._terminate(self.controller_proc)
            self._terminate(self.receiver_proc)
            if self.live_stop_file:
                self.live_stop_file.unlink(missing_ok=True)
                self.live_stop_file = None
            self._set(
                job_id,
                state="failed",
                message=str(exc),
                ended_at=time.time(),
                return_code=-1,
                summary=self.mqtt.events_since(job["started_at"]),
            )
