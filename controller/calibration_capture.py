#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from controller import ExperimentController, load_experiment_config
from settings import RX_DEVICES


def main():
    parser = argparse.ArgumentParser(
        description="Captura finita para calibración/validación ESPectre"
    )
    parser.add_argument("--band", choices=("2.4g", "5g"), required=True)
    parser.add_argument("--devices", default="rx01,rx02,rx03")
    parser.add_argument("--blocks", type=int, required=True)
    args = parser.parse_args()

    if args.blocks < 1 or args.blocks > 120:
        parser.error("--blocks debe estar entre 1 y 120")

    devices = [x.strip() for x in args.devices.split(",") if x.strip()]
    if devices != ["rx01", "rx02", "rx03"]:
        parser.error("calibración ESPectre requiere rx01,rx02,rx03 en ese orden")

    unknown = set(devices) - set(RX_DEVICES)
    if unknown:
        parser.error("receptores desconocidos: " + ", ".join(sorted(unknown)))

    cfg = load_experiment_config()
    cfg["sensing"]["band"] = args.band
    cfg["sensing"]["expected_channel"] = 0
    cfg["capture"]["blocks"] = int(args.blocks)

    controller = ExperimentController(cfg, devices=devices)
    controller.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\\n[CALIBRATION] Interrumpido.")
    except Exception as exc:
        print(f"\\n[FATAL] {exc}")
        sys.exit(1)
