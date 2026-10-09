#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, math, os, signal, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
RECEIVER = ROOT / "controller" / "csi_receiver.py"
CAPTURE = ROOT / "controller" / "calibration_capture.py"
DATASET_ROOT = ROOT / "datasets" / "ae_presence"
LOG_ROOT = ROOT / "test-logs" / "ae_presence"

MAX_BLOCKS_PER_CAPTURE = 120

def env_load():
    p = ROOT / ".env"
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def publish(payload):
    try:
        env_load()
        host = os.getenv("MQTT_HOST", "127.0.0.1")
        port = int(os.getenv("MQTT_PORT", "1883"))
        base = os.getenv("MQTT_BASE_TOPIC", "wifi-sensing/v1").rstrip("/")
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except Exception:
            c = mqtt.Client()
        u = os.getenv("MQTT_SERVER_USER")
        pw = os.getenv("MQTT_SERVER_PASSWORD")
        if u:
            c.username_pw_set(u, pw)
        c.connect(host, port, 30)
        c.loop_start()
        info = c.publish(
            f"{base}/control/ae-session",
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=True,
        )
        info.wait_for_publish(2)
        c.loop_stop()
        c.disconnect()
    except Exception as e:
        print(f"[WARN] no pude publicar estado AE: {e}", flush=True)

def tail(path: Path, n: int = 30) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

def main():
    p = argparse.ArgumentParser(description="Captura una sesión etiquetada para AE one-class")
    p.add_argument("--condition", required=True, choices=["empty", "static", "movement"])
    p.add_argument("--minutes", type=float, required=True)
    p.add_argument("--band", default="2.4g", choices=["2.4g", "5g"])
    p.add_argument("--devices", default="rx01,rx02,rx03")
    p.add_argument("--countdown", type=int, default=10)
    p.add_argument("--name", default=None)
    args = p.parse_args()

    if args.minutes <= 0:
        p.error("--minutes debe ser > 0")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = 0 if args.condition == "empty" else 1
    sid = args.name or f"{args.condition}_{args.band.replace('.','')}_{stamp}"
    sdir = DATASET_ROOT / sid
    samples = sdir / "samples"
    samples.mkdir(parents=True, exist_ok=False)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)

    rlog = LOG_ROOT / f"{sid}_receiver.txt"
    blocks = max(1, int(math.ceil(args.minutes * 60 / 2)))
    chunks = []
    remaining = blocks
    while remaining > 0:
        take = min(MAX_BLOCKS_PER_CAPTURE, remaining)
        chunks.append(take)
        remaining -= take

    meta = {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": sid,
        "condition": args.condition,
        "label": label,
        "band": args.band,
        "devices": [x.strip() for x in args.devices.split(",") if x.strip()],
        "requested_minutes": args.minutes,
        "requested_blocks": blocks,
        "capture_chunks": chunks,
        "feature_contract": "amplitude_3rx_52sc_200hz_ds10_40x156",
        "status": "running",
    }
    (sdir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    publish({
        "event": "SESSION_START",
        "session_id": sid,
        "condition": args.condition,
        "band": args.band,
        "requested_blocks": blocks,
        "chunks": chunks,
        "at": datetime.now(timezone.utc).isoformat(),
    })

    print("=" * 62)
    print(" WIFI SENSING - CAPTURA DATASET AUTOENCODER")
    print("=" * 62)
    print(f"sesión     : {sid}")
    print(f"condición  : {args.condition}")
    print(f"banda      : {args.band}")
    print(f"duración   : {args.minutes:.2f} min")
    print(f"bloques    : {blocks} (~2 s c/u)")
    print(f"tandas     : {' + '.join(map(str, chunks))}")
    print(f"salida     : {sdir}")
    print("=" * 62)

    if args.countdown > 0:
        print(f"Prepara la condición '{args.condition}'. Inicio en {args.countdown} s...")
        for i in range(args.countdown, 0, -1):
            print(f"  {i}", flush=True)
            time.sleep(1)

    rcmd = [
        str(PYTHON), "-u", str(RECEIVER),
        "--ae-capture-dir", str(sdir),
        "--ae-condition", args.condition,
        "--ae-session-id", sid,
        "--ae-devices", args.devices,
    ]

    err = None
    receiver = None

    with rlog.open("w", encoding="utf-8", buffering=1) as rf:
        receiver = subprocess.Popen(
            rcmd,
            cwd=ROOT,
            stdout=rf,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            time.sleep(3.0)
            if receiver.poll() is not None:
                raise RuntimeError(
                    "csi_receiver terminó antes de iniciar la captura.\n"
                    + tail(rlog, 40)
                )

            for idx, chunk_blocks in enumerate(chunks, 1):
                clog = LOG_ROOT / f"{sid}_controller_part{idx:02d}.txt"

                print()
                print(f"[AE] Tanda {idx}/{len(chunks)}: {chunk_blocks} bloques")
                publish({
                    "event": "SESSION_CHUNK_START",
                    "session_id": sid,
                    "chunk": idx,
                    "chunks_total": len(chunks),
                    "blocks": chunk_blocks,
                    "at": datetime.now(timezone.utc).isoformat(),
                })

                ccmd = [
                    str(PYTHON), "-u", str(CAPTURE),
                    "--band", args.band,
                    "--devices", args.devices,
                    "--blocks", str(chunk_blocks),
                ]

                with clog.open("w", encoding="utf-8", buffering=1) as cf:
                    rc = subprocess.call(
                        ccmd,
                        cwd=ROOT,
                        stdout=cf,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )

                if rc != 0:
                    raise RuntimeError(
                        f"calibration_capture terminó con rc={rc} en tanda {idx}.\n"
                        f"--- últimas líneas de {clog.name} ---\n"
                        + tail(clog, 40)
                    )

                saved_now = len(list(samples.glob("*.npz")))
                print(f"[AE] Tanda {idx} terminada. bloques_guardados={saved_now}")

                publish({
                    "event": "SESSION_CHUNK_END",
                    "session_id": sid,
                    "chunk": idx,
                    "chunks_total": len(chunks),
                    "saved_blocks": saved_now,
                    "at": datetime.now(timezone.utc).isoformat(),
                })

                if idx < len(chunks):
                    time.sleep(1.0)

        except Exception as e:
            err = e
        finally:
            if receiver is not None and receiver.poll() is None:
                receiver.send_signal(signal.SIGINT)
                try:
                    receiver.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    receiver.terminate()
                    try:
                        receiver.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        receiver.kill()

    saved = len(list(samples.glob("*.npz")))
    meta["status"] = "ready" if saved > 0 and err is None else "failed"
    meta["saved_blocks"] = saved
    meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    meta["receiver_log"] = str(rlog)
    meta["controller_logs"] = [
        str(LOG_ROOT / f"{sid}_controller_part{i:02d}.txt")
        for i in range(1, len(chunks) + 1)
    ]
    (sdir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    publish({
        "event": "SESSION_END",
        "session_id": sid,
        "condition": args.condition,
        "band": args.band,
        "requested_blocks": blocks,
        "saved_blocks": saved,
        "status": meta["status"],
        "at": meta["finished_at"],
    })

    if err:
        raise err

    print()
    print(f"[OK] sesión={sid} bloques_guardados={saved}/{blocks}")
    print(f"     {sdir}")

if __name__ == "__main__":
    main()
