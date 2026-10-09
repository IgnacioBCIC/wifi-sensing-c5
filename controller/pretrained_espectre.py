#!/usr/bin/env python3


from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import torch
import torch.nn as nn


EXPECTED_DEVICES = ("rx01", "rx02", "rx03")
EXPECTED_FRAMES = 400
EXPECTED_SUBCARRIERS = 56
MODEL_FRAMES = 40
MODEL_FEATURES = 168


class EspectreModelError(RuntimeError):
    pass


class CSINetLight(nn.Module):

    def __init__(self, input_features: int = 168, num_classes: int = 4, dropout: float = 0.3):
        super().__init__()

        self.features = nn.Sequential(
            nn.Conv1d(input_features, 128, kernel_size=7, padding=3),
            nn.BatchNorm1d(128),
            nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Dropout(dropout),

            nn.Conv1d(128, 256, kernel_size=5, padding=2),
            nn.BatchNorm1d(256),
            nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Dropout(dropout),

            nn.Conv1d(256, 128, kernel_size=3, padding=1),
            nn.BatchNorm1d(128),
            nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
        )

        self.classifier = nn.Sequential(
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, num_classes),
        )

    def forward(self, x):

        x = x.transpose(1, 2)
        x = self.features(x)
        x = x.squeeze(-1)
        return self.classifier(x)


@dataclass(frozen=True)
class EspectrePrediction:
    classes: tuple[str, ...]
    probabilities: tuple[float, ...]
    predicted_class: str
    predicted_probability: float
    presence_probability: float
    present: bool
    input_shape: tuple[int, int]
    sampled_sequences: tuple[int, ...]

    def probability_map(self) -> dict[str, float]:
        return dict(zip(self.classes, self.probabilities))


class EspectreZeroShot:

    REQUIRED_FILES = (
        "csi_light_weights.json",
        "baseline.npy",
        "feat_mean.npy",
        "feat_std.npy",
    )

    def __init__(
        self,
        model_dir: str | Path,
        device_order: Sequence[str] = EXPECTED_DEVICES,
        baseline_path: str | Path | None = None,
    ):
        self.model_dir = Path(model_dir)
        self.device_order = tuple(device_order)

        if len(self.device_order) != 3:
            raise EspectreModelError(
                f"CSINetLight espera 3 receptores; recibido={self.device_order}"
            )

        missing = [
            name for name in self.REQUIRED_FILES
            if not (self.model_dir / name).is_file()
        ]
        if missing:
            raise EspectreModelError(
                "Faltan archivos del modelo en "
                f"{self.model_dir}: {missing}"
            )

        self.baseline_path = (
            Path(baseline_path)
            if baseline_path is not None
            else self.model_dir / "baseline.npy"
        )
        if not self.baseline_path.is_file():
            raise EspectreModelError(
                f"baseline no encontrado: {self.baseline_path}"
            )

        self.baseline = np.load(self.baseline_path).astype(np.float32)
        self.feat_mean = np.load(self.model_dir / "feat_mean.npy").astype(np.float32)
        self.feat_std = np.load(self.model_dir / "feat_std.npy").astype(np.float32)

        if self.baseline.shape == (3, 56):
            self.baseline = self.baseline.reshape(-1)
        elif self.baseline.shape != (168,):
            raise EspectreModelError(
                f"baseline.npy shape inesperado: {self.baseline.shape}; "
                "esperado (3,56) o (168,)"
            )

        if self.feat_mean.shape != (168,):
            raise EspectreModelError(
                f"feat_mean.npy shape={self.feat_mean.shape}; esperado=(168,)"
            )
        if self.feat_std.shape != (168,):
            raise EspectreModelError(
                f"feat_std.npy shape={self.feat_std.shape}; esperado=(168,)"
            )

        published_baseline = (self.model_dir / "baseline.npy").resolve()
        selected_baseline = self.baseline_path.resolve()
        if selected_baseline != published_baseline:
            metadata_path = self.baseline_path.parent / "receiver_metadata.json"
            if not metadata_path.is_file():
                raise EspectreModelError(
                    "baseline local sin receiver_metadata.json; "
                    "no se puede verificar compatibilidad LLTF"
                )
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise EspectreModelError(
                    f"metadata de baseline local inválida: {exc}"
                ) from exc
            contract = metadata.get("technical", {}).get("input_contract")
            if contract != "s3_lltf_first56_reconstructed":
                raise EspectreModelError(
                    "baseline local incompatible con el modo LLTF actual: "
                    f"input_contract={contract!r}. Crea un baseline nuevo "
                    "después de flashear LLTF en los 3 receptores."
                )

        self.feat_std = self.feat_std.copy()
        self.feat_std[self.feat_std < 1e-6] = 1.0

        weight_payload = json.loads(
            (self.model_dir / "csi_light_weights.json").read_text()
        )

        classes = weight_payload.get("classes")
        weights = weight_payload.get("weights")

        if not isinstance(classes, list) or not classes:
            raise EspectreModelError("JSON del modelo no contiene 'classes' válido")
        if not isinstance(weights, dict):
            raise EspectreModelError("JSON del modelo no contiene 'weights' válido")

        self.classes = tuple(str(x) for x in classes)

        self.model = CSINetLight(
            input_features=MODEL_FEATURES,
            num_classes=len(self.classes),
        )

        state = self.model.state_dict()

        loaded = {}
        for key, reference in state.items():
            if key not in weights:
                raise EspectreModelError(
                    f"Peso ausente en JSON: {key}"
                )

            arr = np.asarray(weights[key])

            expected_numel = reference.numel()
            if arr.size != expected_numel:
                raise EspectreModelError(
                    f"{key}: elementos={arr.size}, esperado={expected_numel}"
                )


            tensor = torch.as_tensor(arr.reshape(tuple(reference.shape)))
            tensor = tensor.to(dtype=reference.dtype)
            loaded[key] = tensor

        extra = sorted(set(weights) - set(state))
        if extra:

            print(
                "[ESPECTRE] Aviso: pesos extra ignorados: "
                + ", ".join(extra[:10])
            )

        self.model.load_state_dict(loaded, strict=True)
        self.model.eval()

        if "empty" not in self.classes:
            raise EspectreModelError(
                f"El modelo no contiene clase 'empty': {self.classes}"
            )

    @staticmethod
    def _extract_amplitude(block) -> np.ndarray:
        if not getattr(block, "model_ready", False):
            raise EspectreModelError(
                f"block={getattr(block, 'block_id', '?')} no es model-ready"
            )

        matrix_source = getattr(
            block,
            "espectre_amplitude_matrix",
            None,
        )
        if matrix_source is None:
            raise EspectreModelError(
                f"block={getattr(block, 'block_id', '?')}: "
                "falta espectre_amplitude_matrix ESPectre56; "
                "no se permite usar amplitude_matrix analítica como fallback"
            )

        matrix = np.asarray(matrix_source, dtype=np.float32)

        if matrix.shape != (EXPECTED_FRAMES, EXPECTED_SUBCARRIERS):
            raise EspectreModelError(
                f"block={block.block_id}: shape={matrix.shape}; "
                f"esperado={(EXPECTED_FRAMES, EXPECTED_SUBCARRIERS)}"
            )

        if not np.isfinite(matrix).all():
            raise EspectreModelError(
                f"block={block.block_id}: amplitudes no finitas"
            )

        return matrix

    def adapt_blocks(
        self,
        blocks_by_device: Mapping[str, object],
    ) -> tuple[np.ndarray, tuple[int, ...]]:

        absent = [
            dev for dev in self.device_order
            if dev not in blocks_by_device
        ]
        if absent:
            raise EspectreModelError(
                f"faltan receptores para inferencia: {absent}"
            )

        matrices = [
            self._extract_amplitude(blocks_by_device[dev])
            for dev in self.device_order
        ]

        block_ids = {
            int(blocks_by_device[dev].block_id)
            for dev in self.device_order
        }
        if len(block_ids) != 1:
            raise EspectreModelError(
                f"block_id no sincronizado entre receptores: {sorted(block_ids)}"
            )


        stacked = np.stack(matrices, axis=1)


        sample_idx = np.arange(0, EXPECTED_FRAMES, 10, dtype=np.int64)

        if sample_idx.size != MODEL_FRAMES:
            raise EspectreModelError(
                f"adaptación temporal produjo {sample_idx.size} frames"
            )

        sampled = stacked[sample_idx]
        flat = sampled.reshape(MODEL_FRAMES, MODEL_FEATURES)


        norms = np.linalg.norm(flat, axis=1, keepdims=True)
        norms[norms < 1e-6] = 1.0
        flat = flat / norms


        flat = flat - self.baseline[np.newaxis, :]
        flat = (flat - self.feat_mean[np.newaxis, :]) / self.feat_std[np.newaxis, :]

        if not np.isfinite(flat).all():
            raise EspectreModelError(
                "adaptador produjo NaN/Inf"
            )

        return flat.astype(np.float32), tuple(int(x) for x in sample_idx)

    def predict_blocks(
        self,
        blocks_by_device: Mapping[str, object],
    ) -> EspectrePrediction:
        features, sampled_sequences = self.adapt_blocks(blocks_by_device)

        x = torch.from_numpy(features).unsqueeze(0)

        with torch.inference_mode():
            logits = self.model(x)
            probs = torch.softmax(logits, dim=1)[0].cpu().numpy()

        pred_idx = int(np.argmax(probs))
        pmap = dict(zip(self.classes, probs.tolist()))

        p_empty = float(pmap["empty"])
        p_presence = 1.0 - p_empty

        return EspectrePrediction(
            classes=self.classes,
            probabilities=tuple(float(x) for x in probs),
            predicted_class=self.classes[pred_idx],
            predicted_probability=float(probs[pred_idx]),
            presence_probability=p_presence,
            present=self.classes[pred_idx] != "empty",
            input_shape=(MODEL_FRAMES, MODEL_FEATURES),
            sampled_sequences=sampled_sequences,
        )
