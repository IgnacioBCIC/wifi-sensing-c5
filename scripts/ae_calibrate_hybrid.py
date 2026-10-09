#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

AE_PRED_RE = re.compile(r"^\[AE-PRED\]\s+(\{.*\})\s*$")


def parse_log(path: Path):
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = AE_PRED_RE.match(line.strip())
        if not m:
            continue
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        branches = obj.get("branches") or {}
        spectral = branches.get("spectral") or {}
        delta = branches.get("delta") or {}
        if "score_mean" not in spectral or "score_mean" not in delta:
            continue
        rows.append({
            "file": str(path),
            "line": lineno,
            "block_id": obj.get("block_id"),
            "spectral": float(spectral["score_mean"]),
            "delta": float(delta["score_mean"]),
        })
    return rows


def percentile(values, q):
    xs = sorted(float(x) for x in values)
    if not xs:
        raise ValueError("lista vacía")
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return xs[lo] * (1 - w) + xs[hi] * w


def stats(values):
    xs = [float(x) for x in values]
    return {
        "n": len(xs),
        "min": min(xs),
        "mean": sum(xs) / len(xs),
        "p95": percentile(xs, 0.95),
        "p99": percentile(xs, 0.99),
        "max": max(xs),
    }


def main():
    p = argparse.ArgumentParser(
        description="Calibra thresholds efectivos de un AE híbrido usando SOLO logs EMPTY."
    )
    p.add_argument("--model", required=True, help="Directorio del modelo híbrido.")
    p.add_argument(
        "--empty-log",
        action="append",
        required=True,
        help="Log EMPTY. Puede repetirse varias veces.",
    )
    p.add_argument(
        "--spectral-margin",
        type=float,
        default=1.20,
        help="Margen multiplicativo sobre el estadístico EMPTY spectral. Default 1.20.",
    )
    p.add_argument(
        "--delta-margin",
        type=float,
        default=1.10,
        help="Margen multiplicativo sobre el estadístico EMPTY delta. Default 1.10.",
    )
    p.add_argument(
        "--stat",
        choices=["max", "p99"],
        default="max",
        help="Base para calibración. max es más conservador con muestras pequeñas.",
    )
    p.add_argument(
        "--min-blocks",
        type=int,
        default=15,
        help="Mínimo total de bloques EMPTY requeridos.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Sólo calcula y muestra; no modifica metadata.json.",
    )
    args = p.parse_args()

    if args.spectral_margin <= 1.0 or args.delta_margin <= 1.0:
        p.error("Los márgenes deben ser > 1.0")

    model = Path(args.model).expanduser().resolve()
    metadata_path = model / "metadata.json"
    if not metadata_path.is_file():
        raise SystemExit(f"[ERROR] No existe {metadata_path}")

    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    if meta.get("model_type") != "hybrid_dual_branch_v1":
        raise SystemExit(
            f"[ERROR] modelo no es hybrid_dual_branch_v1: {meta.get('model_type')}"
        )

    rows = []
    logs = []
    for raw in args.empty_log:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"[ERROR] No existe log: {path}")
        parsed = parse_log(path)
        if not parsed:
            raise SystemExit(f"[ERROR] No encontré AE-PRED híbridos en {path}")
        rows.extend(parsed)
        logs.append(str(path))

    if len(rows) < args.min_blocks:
        raise SystemExit(
            f"[ERROR] Sólo hay {len(rows)} bloques EMPTY; mínimo requerido={args.min_blocks}"
        )

    spectral_values = [r["spectral"] for r in rows]
    delta_values = [r["delta"] for r in rows]
    s_stats = stats(spectral_values)
    d_stats = stats(delta_values)

    base_s = s_stats[args.stat]
    base_d = d_stats[args.stat]
    new_s = float(base_s * args.spectral_margin)
    new_d = float(base_d * args.delta_margin)

    old_s = float(meta["branches"]["spectral"]["effective_threshold"])
    old_d = float(meta["branches"]["delta"]["effective_threshold"])

    print("=" * 72)
    print(" CALIBRACIÓN AE HÍBRIDO — EMPTY")
    print("=" * 72)
    print(f"modelo              : {model}")
    print(f"logs EMPTY          : {len(logs)}")
    print(f"bloques             : {len(rows)}")
    print(f"estadístico base    : {args.stat}")
    print()
    print("SPECTRAL")
    print(f"  mean              : {s_stats['mean']:.6f}")
    print(f"  p95               : {s_stats['p95']:.6f}")
    print(f"  p99               : {s_stats['p99']:.6f}")
    print(f"  max               : {s_stats['max']:.6f}")
    print(f"  threshold anterior: {old_s:.6f}")
    print(f"  margen            : {args.spectral_margin:.3f}x")
    print(f"  threshold nuevo   : {new_s:.6f}")
    print()
    print("DELTA")
    print(f"  mean              : {d_stats['mean']:.6f}")
    print(f"  p95               : {d_stats['p95']:.6f}")
    print(f"  p99               : {d_stats['p99']:.6f}")
    print(f"  max               : {d_stats['max']:.6f}")
    print(f"  threshold anterior: {old_d:.6f}")
    print(f"  margen            : {args.delta_margin:.3f}x")
    print(f"  threshold nuevo   : {new_d:.6f}")
    print("=" * 72)

    report = {
        "schema_version": 1,
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "model_id": meta.get("model_id"),
        "empty_logs": logs,
        "empty_blocks": len(rows),
        "statistic": args.stat,
        "spectral": {
            "stats": s_stats,
            "margin": args.spectral_margin,
            "old_threshold": old_s,
            "new_threshold": new_s,
        },
        "delta": {
            "stats": d_stats,
            "margin": args.delta_margin,
            "old_threshold": old_d,
            "new_threshold": new_d,
        },
        "note": (
            "Calibración EMPTY operativa. No sustituye validación final con múltiples "
            "sesiones EMPTY/STATIC/MOVEMENT independientes."
        ),
    }

    report_path = model / "calibration_report.json"

    if args.dry_run:
        print("[DRY-RUN] No se modificó metadata.json.")
        print(json.dumps(report, indent=2))
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = model / f"metadata.before_calibration_{stamp}.json"
    shutil.copy2(metadata_path, backup)

    meta["branches"]["spectral"]["effective_threshold"] = new_s
    meta["branches"]["delta"]["effective_threshold"] = new_d
    meta["calibration"] = report
    meta["threshold_rationale"] = {
        "status": "empty_live_calibrated",
        "statistic": args.stat,
        "spectral_margin": args.spectral_margin,
        "delta_margin": args.delta_margin,
        "empty_blocks": len(rows),
        "empty_logs": logs,
        "note": (
            "Thresholds recalibrados con logs EMPTY live. "
            "Repetir al cambiar entorno físico/banda/configuración relevante."
        ),
    }

    metadata_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[OK] metadata actualizado : {metadata_path}")
    print(f"[OK] backup               : {backup}")
    print(f"[OK] reporte              : {report_path}")


if __name__ == "__main__":
    main()
