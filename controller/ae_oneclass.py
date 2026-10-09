#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

try:
    import torch
    import torch.nn as nn
except Exception as exc:
    raise RuntimeError("PyTorch es requerido para el autoencoder one-class") from exc

FEATURES = 156
RX_COUNT = 3
SC_PER_RX = 52
SAMPLED_SEQUENCES = tuple(range(0, 400, 10))
RAW_FEATURE_CONTRACT = "amplitude_3rx_52sc_200hz_ds10_40x156"
SPECTRAL_PREPROCESSING_ID = "relative_per_row_rx_zscore_v1"
DELTA_PREPROCESSING_ID = "relative_then_first_difference_v1"
HYBRID_MODEL_TYPE = "hybrid_dual_branch_v1"


def spectral_relative_block(x: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.shape != (40, FEATURES):
        raise ValueError(f"shape AE inesperado: {x.shape}; esperado (40,{FEATURES})")
    if not np.isfinite(x).all():
        raise ValueError("bloque AE contiene NaN/Inf")
    y = x.reshape(40, RX_COUNT, SC_PER_RX)
    mean = y.mean(axis=2, keepdims=True)
    std = y.std(axis=2, keepdims=True)
    std = np.where(std < eps, 1.0, std).astype(np.float32)
    out = ((y - mean) / std).reshape(40, FEATURES).astype(np.float32)
    if not np.isfinite(out).all():
        raise ValueError("normalización espectral produjo NaN/Inf")
    return out


def temporal_delta_block(x: np.ndarray) -> np.ndarray:
    rel = spectral_relative_block(x).reshape(40, RX_COUNT, SC_PER_RX)
    delta = np.empty_like(rel, dtype=np.float32)
    delta[0] = 0.0
    delta[1:] = rel[1:] - rel[:-1]
    return delta.reshape(40, FEATURES).astype(np.float32)


def relative_normalize_block(x: np.ndarray) -> np.ndarray:
    return temporal_delta_block(x)


class OneClassAutoencoder(nn.Module):
    def __init__(self, features: int = FEATURES):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(features, 96), nn.ReLU(),
            nn.Linear(96, 32), nn.ReLU(),
            nn.Linear(32, 12),
        )
        self.decoder = nn.Sequential(
            nn.Linear(12, 32), nn.ReLU(),
            nn.Linear(32, 96), nn.ReLU(),
            nn.Linear(96, features),
        )

    def forward(self, x):
        return self.decoder(self.encoder(x))


@dataclass(frozen=True)
class AEPrediction:
    score_mean: float
    score_median: float
    score_p95: float
    threshold: float
    present: bool
    ratio: float
    branches: dict[str, Any] = field(default_factory=dict)


class _BranchRuntime:
    def __init__(self, *, model_dir: Path, prefix: str, preprocessing: str,
                 base_threshold: float, effective_threshold: float):
        self.preprocessing = preprocessing
        self.base_threshold = float(base_threshold)
        self.threshold = float(effective_threshold)
        weights = model_dir / f"model_{prefix}.pt"
        normalizer = model_dir / f"normalizer_{prefix}.npz"
        for p in (weights, normalizer):
            if not p.is_file():
                raise RuntimeError(f"archivo rama AE no encontrado: {p}")
        norm = np.load(normalizer)
        self.mean = np.asarray(norm["mean"], dtype=np.float32)
        self.std = np.asarray(norm["std"], dtype=np.float32)
        self.std = np.where(self.std < 1e-6, 1.0, self.std).astype(np.float32)
        self.device = torch.device("cpu")
        self.model = OneClassAutoencoder(FEATURES).to(self.device)
        state = torch.load(weights, map_location=self.device, weights_only=True)
        self.model.load_state_dict(state)
        self.model.eval()

    def transform(self, x: np.ndarray) -> np.ndarray:
        if self.preprocessing == SPECTRAL_PREPROCESSING_ID:
            return spectral_relative_block(x)
        if self.preprocessing == DELTA_PREPROCESSING_ID:
            return temporal_delta_block(x)
        raise RuntimeError(f"preprocessing híbrido desconocido: {self.preprocessing}")

    def score(self, x: np.ndarray) -> dict[str, float | bool]:
        feat = self.transform(x)
        z = (feat - self.mean) / self.std
        with torch.no_grad():
            xt = torch.from_numpy(z.astype(np.float32)).to(self.device)
            xr = self.model(xt)
            row = torch.mean((xr - xt) ** 2, dim=1).cpu().numpy()
        mean = float(np.mean(row))
        median = float(np.median(row))
        p95 = float(np.percentile(row, 95))
        return {
            "score_mean": mean,
            "score_median": median,
            "score_p95": p95,
            "base_threshold": self.base_threshold,
            "threshold": self.threshold,
            "ratio": float(mean / self.threshold) if self.threshold > 0 else float("inf"),
            "present": bool(mean > self.threshold),
        }


class OneClassAERuntime:
    def __init__(self, model_dir: str | Path):
        self.model_dir = Path(model_dir)
        metadata_path = self.model_dir / "metadata.json"
        if not metadata_path.is_file():
            raise RuntimeError(f"metadata AE no encontrado: {metadata_path}")
        self.metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.device_order = tuple(self.metadata.get("device_order", ["rx01", "rx02", "rx03"]))
        self.input_features = int(self.metadata.get("input_features", FEATURES))
        self.model_type = self.metadata.get("model_type", "single_branch")

        if self.model_type == HYBRID_MODEL_TYPE:
            branches = self.metadata["branches"]
            s = branches["spectral"]
            d = branches["delta"]
            self.spectral = _BranchRuntime(
                model_dir=self.model_dir, prefix="spectral",
                preprocessing=s["preprocessing"],
                base_threshold=s["base_threshold"],
                effective_threshold=s["effective_threshold"],
            )
            self.delta = _BranchRuntime(
                model_dir=self.model_dir, prefix="delta",
                preprocessing=d["preprocessing"],
                base_threshold=d["base_threshold"],
                effective_threshold=d["effective_threshold"],
            )
            self.threshold = 1.0
            return

        normalizer_path = self.model_dir / "normalizer.npz"
        weights_path = self.model_dir / "model.pt"
        for p in (normalizer_path, weights_path):
            if not p.is_file():
                raise RuntimeError(f"archivo AE no encontrado: {p}")
        norm = np.load(normalizer_path)
        self.mean = np.asarray(norm["mean"], dtype=np.float32)
        self.std = np.asarray(norm["std"], dtype=np.float32)
        self.std = np.where(self.std < 1e-6, 1.0, self.std).astype(np.float32)
        self.threshold = float(self.metadata["threshold"])
        self.device = torch.device("cpu")
        self.model = OneClassAutoencoder(self.input_features).to(self.device)
        state = torch.load(weights_path, map_location=self.device, weights_only=True)
        self.model.load_state_dict(state)
        self.model.eval()

    def predict(self, x: np.ndarray) -> AEPrediction:
        x = np.asarray(x, dtype=np.float32)
        if x.shape != (40, FEATURES):
            raise ValueError(f"shape AE inesperado: {x.shape}; esperado (40,{FEATURES})")

        if self.model_type == HYBRID_MODEL_TYPE:
            s = self.spectral.score(x)
            d = self.delta.score(x)
            present = bool(s["present"] or d["present"])
            combined_ratio = float(max(s["ratio"], d["ratio"]))
            return AEPrediction(
                score_mean=combined_ratio,
                score_median=combined_ratio,
                score_p95=combined_ratio,
                threshold=1.0,
                present=present,
                ratio=combined_ratio,
                branches={"spectral": s, "delta": d},
            )

        feat = relative_normalize_block(x)
        z = (feat - self.mean) / self.std
        with torch.no_grad():
            xt = torch.from_numpy(z.astype(np.float32)).to(self.device)
            xr = self.model(xt)
            row = torch.mean((xr - xt) ** 2, dim=1).cpu().numpy()
        score_mean = float(np.mean(row))
        score_median = float(np.median(row))
        score_p95 = float(np.percentile(row, 95))
        return AEPrediction(
            score_mean=score_mean,
            score_median=score_median,
            score_p95=score_p95,
            threshold=self.threshold,
            present=bool(score_mean > self.threshold),
            ratio=float(score_mean / self.threshold) if self.threshold > 0 else float("inf"),
        )


def build_ae_block(bucket: dict[str, dict[str, Any]], device_order=("rx01", "rx02", "rx03")) -> tuple[np.ndarray, dict]:
    matrices = []
    rssis = {}
    for dev in device_order:
        if dev not in bucket:
            raise ValueError(f"falta {dev} en bucket AE")
        processed = bucket[dev]["processed"]
        matrix = np.asarray(processed.amplitude_matrix, dtype=np.float32)
        if matrix.shape != (400, 52):
            raise ValueError(f"{dev}: shape amplitud {matrix.shape}; esperado (400,52)")
        matrices.append(matrix[list(SAMPLED_SEQUENCES), :])
        rssi = np.asarray(getattr(processed, "rssi", ()), dtype=np.float32)
        finite = rssi[np.isfinite(rssi)]
        rssis[dev] = float(finite.mean()) if finite.size else None
    x = np.concatenate(matrices, axis=1).astype(np.float32)
    if x.shape != (40, FEATURES) or not np.isfinite(x).all():
        raise ValueError(f"bloque AE inválido: shape={x.shape}")
    return x, {"rssi_mean": rssis, "sampled_sequences": list(SAMPLED_SEQUENCES)}
