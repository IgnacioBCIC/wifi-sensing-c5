
from __future__ import annotations

import atexit
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from services.config_service import ExperimentConfigService
from services.database import Database
from services.job_manager import JobManager
from services.mqtt_state import MqttState


PROJECT_ROOT = Path(__file__).resolve().parent.parent

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

config_service = ExperimentConfigService()
database = Database()
mqtt_state = MqttState()
job_manager = JobManager(mqtt_state, database, config_service)

mqtt_state.start()


@atexit.register
def _shutdown():
    job_manager.stop_all()
    mqtt_state.stop()


@app.get("/")
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.get("/config")
def config_page():
    return render_template(
        "config.html",
        active="config",
        config=config_service.editable_snapshot(),
    )


@app.get("/calibration")
def calibration_page():
    return render_template(
        "calibration.html",
        active="calibration",
    )


@app.get("/history")
def history_page():
    return render_template(
        "history.html",
        active="history",
        jobs=database.recent(50),
    )


@app.get("/api/status")
def api_status():
    current = job_manager.get_current()
    return jsonify(
        {
            "system": mqtt_state.snapshot(),
            "job": current,
            "config": config_service.derived(config_service.load()),
        }
    )


@app.get("/api/config")
def api_config():
    return jsonify(config_service.editable_snapshot())


@app.put("/api/config")
def api_config_update():
    payload = request.get_json(silent=True) or {}
    values = payload.get("values")
    if not isinstance(values, dict):
        return jsonify({"error": "values debe ser un objeto"}), 400
    if job_manager.is_busy():
        return jsonify(
            {"error": "No se puede modificar experiment.json con un trabajo activo."}
        ), 409
    try:
        updated = config_service.update(values)
        return jsonify(updated)
    except (ValueError, KeyError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


def _devices(payload):
    devices = payload.get("devices", ["rx01", "rx02", "rx03"])
    if not isinstance(devices, list):
        raise ValueError("devices debe ser una lista")
    devices = [str(x).strip() for x in devices if str(x).strip()]
    allowed = {"rx01", "rx02", "rx03"}
    if not devices or set(devices) - allowed:
        raise ValueError("Receptores inválidos")
    return devices


@app.post("/api/jobs/health")
def api_start_health():
    payload = request.get_json(silent=True) or {}
    band = payload.get("band", "2.4g")
    if band not in {"2.4g", "5g"}:
        return jsonify({"error": "Banda inválida"}), 400
    try:
        job = job_manager.start_health(
            band=band,
            devices=_devices(payload),
        )
        return jsonify(job), 202
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.post("/api/jobs/experiment")
def api_start_experiment():
    payload = request.get_json(silent=True) or {}
    band = payload.get("band", "2.4g")
    baseline_id = str(payload.get("baseline_id", "published")).strip()
    model_mode = payload.get("model_mode", "zero_shot_a")
    countdown_s = int(payload.get("countdown_s", 5))

    if band not in {"2.4g", "5g"}:
        return jsonify({"error": "Banda inválida"}), 400
    if model_mode not in {"zero_shot_a", "ae_hybrid_v1", "none"}:
        return jsonify({"error": "Modo de modelo inválido"}), 400
    if not 0 <= countdown_s <= 30:
        return jsonify({"error": "countdown_s debe estar entre 0 y 30"}), 400

    try:
        job = job_manager.start_experiment(
            band=band,
            devices=_devices(payload),
            baseline_id=baseline_id,
            model_mode=model_mode,
            countdown_s=countdown_s,
        )
        return jsonify(job), 202
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.post("/api/jobs/live")
def api_start_live():
    payload = request.get_json(silent=True) or {}
    band = payload.get("band", "2.4g")
    baseline_id = str(payload.get("baseline_id", "published")).strip()
    model_mode = payload.get("model_mode", "zero_shot_a")
    countdown_s = int(payload.get("countdown_s", 5))

    if band not in {"2.4g", "5g"}:
        return jsonify({"error": "Banda inválida"}), 400
    if model_mode not in {"zero_shot_a", "ae_hybrid_v1", "none"}:
        return jsonify({"error": "Modo de modelo inválido"}), 400
    if not 0 <= countdown_s <= 30:
        return jsonify({"error": "countdown_s debe estar entre 0 y 30"}), 400

    try:
        job = job_manager.start_live(
            band=band,
            devices=_devices(payload),
            baseline_id=baseline_id,
            model_mode=model_mode,
            countdown_s=countdown_s,
        )
        return jsonify(job), 202
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.get("/api/calibrations")
def api_calibrations():
    return jsonify(job_manager.calibrations.snapshot())


@app.post("/api/calibrations/start")
def api_calibration_start():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    band = payload.get("band", "2.4g")
    note = str(payload.get("note", "")).strip()
    countdown_s = int(payload.get("countdown_s", 5))

    if band not in {"2.4g", "5g"}:
        return jsonify({"error": "Banda inválida"}), 400
    if not 0 <= countdown_s <= 30:
        return jsonify({"error": "countdown_s debe estar entre 0 y 30"}), 400

    try:
        job = job_manager.start_calibration(
            name=name,
            band=band,
            note=note,
            countdown_s=countdown_s,
        )
        return jsonify(job), 202
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.post("/api/calibrations/published")
def api_calibration_published():
    if job_manager.is_busy():
        return jsonify({"error": "No se puede cambiar baseline con un trabajo activo."}), 409
    payload = request.get_json(silent=True) or {}
    band = payload.get("band")
    try:
        return jsonify(job_manager.calibrations.use_published(band))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/calibrations/<profile_id>/activate")
def api_calibration_activate(profile_id):
    if job_manager.is_busy():
        return jsonify({"error": "No se puede cambiar baseline con un trabajo activo."}), 409
    try:
        return jsonify(job_manager.calibrations.activate(profile_id))
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/calibrations/<profile_id>/validate")
def api_calibration_validate(profile_id):
    payload = request.get_json(silent=True) or {}
    condition = payload.get("condition", "empty")
    countdown_s = int(payload.get("countdown_s", 5))
    if condition not in {"empty", "presence_static"}:
        return jsonify({"error": "Condición inválida"}), 400
    if not 0 <= countdown_s <= 30:
        return jsonify({"error": "countdown_s debe estar entre 0 y 30"}), 400
    try:
        job = job_manager.start_calibration_validation(
            profile_id=profile_id,
            condition=condition,
            countdown_s=countdown_s,
        )
        return jsonify(job), 202
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.get("/api/ae/models")
def api_ae_models():
    return jsonify(job_manager.ae_models_snapshot())


@app.post("/api/ae/calibrate")
def api_ae_calibrate():
    payload = request.get_json(silent=True) or {}
    band = payload.get("band", "2.4g")
    countdown_s = int(payload.get("countdown_s", 5))
    target_blocks = int(payload.get("target_blocks", 20))

    if band not in {"2.4g", "5g"}:
        return jsonify({"error": "Banda inválida"}), 400
    if not 0 <= countdown_s <= 30:
        return jsonify({"error": "countdown_s debe estar entre 0 y 30"}), 400
    if not 15 <= target_blocks <= 120:
        return jsonify({"error": "target_blocks debe estar entre 15 y 120"}), 400

    try:
        job = job_manager.start_ae_calibration(
            band=band,
            countdown_s=countdown_s,
            target_blocks=target_blocks,
        )
        return jsonify(job), 202
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.post("/api/jobs/cancel")
def api_cancel():
    try:
        return jsonify(job_manager.cancel())
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@app.get("/api/jobs/current")
def api_current():
    return jsonify(job_manager.get_current())


@app.get("/api/history")
def api_history():
    return jsonify(database.recent(50))


@app.get("/api/history/<job_id>")
def api_history_one(job_id):
    item = database.get(job_id)
    if item is None:
        return jsonify({"error": "No encontrado"}), 404
    return jsonify(item)


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        threaded=True,
        use_reloader=False,
    )
