"""Speaker embeddings: a voice turned into a unit vector, compared by cosine.

The listener uses this to tell the enrolled owner from everyone else. The
model is an ONNX export from the sherpa-onnx release (speaker_models.py),
run on onnxruntime, which the listener already depends on; the features
come from fbank.py. No PyTorch.

numpy and onnxruntime are imported inside the functions: modules in this
package must import under a bare python3 (tests/test_plugin_layout.py).
"""

from __future__ import annotations

from pathlib import Path

from . import fbank

MIN_FRAMES = 10  # 100 ms of features; anything shorter has no voice in it


class SpeakerError(RuntimeError):
    pass


class Embedder:
    """One loaded model. `embed` is not thread-safe; use one per thread.

    `normalise="model"` (the default) applies the per-segment feature mean
    normalisation the model was trained with. sherpa-onnx applies it only when
    the export says `feature_normalize_type=global-mean`, which its WeSpeaker
    exports do not. `normalise="sherpa"` reproduces sherpa-onnx exactly, for
    parity tests.
    """

    def __init__(self, model: Path | str, threads: int = 1, normalise: str = "model") -> None:
        import onnxruntime as ort

        if normalise not in ("model", "sherpa"):
            raise ValueError(f"normalise must be 'model' or 'sherpa', not {normalise!r}")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(model), sess_options=options, providers=["CPUExecutionProvider"]
        )
        meta = self._session.get_modelmeta().custom_metadata_map
        self.framework = meta.get("framework", "")
        self.dim = int(meta.get("output_dim", self._session.get_outputs()[0].shape[-1]))
        if int(meta.get("sample_rate", fbank.SAMPLE_RATE)) != fbank.SAMPLE_RATE:
            raise SpeakerError(f"{model}: the model is not for {fbank.SAMPLE_RATE} Hz audio")
        # normalize_samples=0: the model was trained on 16-bit-range samples.
        self.scale = 1.0 if meta.get("normalize_samples", "1") == "1" else 32768.0
        global_mean = meta.get("feature_normalize_type") == "global-mean"
        if normalise == "sherpa":
            self.mean_normalise = global_mean
        else:
            self.mean_normalise = global_mean or self.framework == "wespeaker"
        self._input = self._session.get_inputs()[0].name
        self.preprocessing = (
            f"{fbank.VERSION};scale={self.scale:g};mean={'on' if self.mean_normalise else 'off'}"
        )

    def embed(self, samples):
        """Unit-length float32 embedding of 16 kHz float samples in [-1, 1]."""
        import numpy as np

        features = fbank.fbank(np.asarray(samples, dtype=np.float32) * self.scale)
        if len(features) < MIN_FRAMES:
            raise SpeakerError(f"too short to embed: {len(features)} frames")
        if self.mean_normalise:
            features = features - features.mean(axis=0, keepdims=True)
        (vector,) = self._session.run(None, {self._input: features[None]})[0]
        vector = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm == 0:
            raise SpeakerError("the model returned an empty embedding")
        return vector / norm


def centroid(vectors):
    """Unit-length mean of unit-length embeddings."""
    import numpy as np

    stacked = np.stack([np.asarray(v, dtype=np.float32) for v in vectors])
    mean = stacked.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0:
        raise SpeakerError("embeddings cancel out; enrollment is unusable")
    return (mean / norm).astype(np.float32)


def score(a, b) -> float:
    """Cosine similarity of two unit-length embeddings."""
    import numpy as np

    return float(np.dot(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)))
