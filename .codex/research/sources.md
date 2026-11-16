# Sources and evidence notes

Accessed 2026-11-16. Research used live HTTPS retrieval of upstream documentation and source code, followed references to model catalogs, and compared those findings with DONNA’s code. No general web-search service was available. This is a targeted primary-source review, not an exhaustive literature search or an experimental benchmark. Repository branch URLs are mutable; the accompanying `source-manifest.json` records SHA-256 digests of the retrieved documents to identify the content reviewed. It does not archive the documents or pin repository commits.

## Inspected primary sources

| ID | Source | Evidence used and limits |
| --- | --- | --- |
| S1 | [WeSpeaker README](https://github.com/wenet-e2e/wespeaker/blob/master/README.md) | Speaker embeddings, similarity and diarization interfaces; Apache-2.0 code license. Toolkit capability does not establish deployed performance. |
| S2 | [WeSpeaker pretrained model catalog](https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md) | ONNX exports, ResNet/CAM++/ECAPA options, 16 kHz inference example, dataset-dependent model licensing. Notes LM variants may favor longer audio, e.g. over 3 seconds. Exact artifact downloads were not attempted. |
| S3 | [sherpa-onnx speaker identification example](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/speaker-identification.py) | `SpeakerEmbeddingExtractor`, averaged enrollment embeddings, `SpeakerEmbeddingManager`, unknown fallback, example threshold 0.6; provider options cpu/cuda/coreml. Example code was read, not executed; it opens a microphone and should not be used as a DONNA test unchanged. |
| S4 | [SpeechBrain VoxCeleb recipe](https://github.com/speechbrain/speechbrain/blob/develop/recipes/VoxCeleb/SpeakerRec/README.md) | ECAPA training/verification, published EER results and links to pretrained encoders. The Hugging Face model card itself was blocked. |
| S5 | [SpeechBrain verification implementation](https://github.com/speechbrain/speechbrain/blob/develop/speechbrain/inference/speaker.py) | `SpeakerRecognition`, 16 kHz input, cosine scoring, default 0.25 threshold. Despite some docstrings saying “distance,” the implementation uses `torch.nn.CosineSimilarity` and accepts `score > threshold`. |
| S6 | [pyannote.audio README](https://github.com/pyannote/pyannote-audio/blob/develop/README.md) | Community-1 local execution and access conditions, Precision-2 server execution, DER benchmarks, H100 speed measurements, optional telemetry and disabling it. No claim of measured Apple Silicon streaming performance. |
| S7 | [diart README and paper summary](https://github.com/juanmc2005/diart/blob/main/README.md) | Online overlap-aware diarization, 500 ms rolling updates, 0.5–5 s configurable latency, benchmark tooling. Read the README’s paper summary; the full paper was not separately reviewed. |
| S8 | [Picovoice Eagle README](https://github.com/Picovoice/eagle/blob/main/README.md) | Enrollment/recognition profiles, real-time local processing, macOS arm64 support, AccessKey and internet license validation. Vendor accuracy claims were not independently established; website pricing/benchmark docs were blocked. |
| S9 | [Resemblyzer README](https://github.com/resemble-ai/Resemblyzer/blob/master/README.md) | 256-dimensional voice embeddings, reference-voice matching, PyTorch, English preference and limitations of the small fake-speech example. GPU timing claims were not extrapolated to DONNA. |
| S10 | [ASVspoof 2021 baseline/evaluation repository](https://github.com/asvspoof-challenge/2021/blob/main/README.md) | Separate spoof countermeasures and evaluation tooling, including EER/min t-DCF and LA/PA/DF tracks. Historical benchmark context, not the latest complete survey. |
| S11 | [ASVspoof 5 baseline repository](https://github.com/asvspoof-challenge/asvspoof5/blob/main/README.md) | RawNet2/AASIST countermeasures, separate spoof-aware speaker verification baselines, and distinct track metrics. No detector installed or evaluated. |
| S12 | [whisper.cpp README](https://github.com/ggml-org/whisper.cpp/blob/master/README.md) | Local ASR and Apple Silicon support. Existing DONNA code establishes the actual current transcription integration. ASR support is not owner enrollment/verification. |
| S13 | [sherpa-onnx license](https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE) | Apache-2.0 runtime code license; not blanket permission for all downloadable models. |
| S14 | [SpeechBrain license](https://github.com/speechbrain/speechbrain/blob/develop/LICENSE) | Apache-2.0 toolkit code license. |
| S15 | [pyannote.audio license](https://github.com/pyannote/pyannote-audio/blob/develop/LICENSE) | MIT toolkit code license; model terms require separate review. |

## Access limitations

The following requests returned HTTP 403 with `Approval required for <host>:443`. Their bodies were read, and their contents were **not** treated as inspected evidence:

- `https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb/raw/main/README.md`
- `https://huggingface.co/pyannote/speaker-diarization-community-1/raw/main/README.md`
- `https://k2-fsa.github.io/sherpa/onnx/speaker-identification/index.html`
- `https://picovoice.ai/docs/quick-start/eagle-python/`
- `https://developers.deepgram.com/docs/diarization`
- `https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speaker-recognition-overview`
- `https://www.asvspoof.org/index2021.html`
- `https://pages.nist.gov/800-63-4/sp800-63b.html`
- `https://arxiv.org/abs/2005.07143`

Accessible upstream GitHub documentation supplied enough evidence for the local architecture recommendation, so research continued without requiring an access change. No current Deepgram/Azure capability, NIST normative requirement, vendor price, or full-paper finding is asserted based on these blocked pages. If expanding this research, host-side pending approvals can be reviewed with `sbx policy approval ls`, inspected with `sbx policy approval inspect <approval-id>`, and answered with `sbx policy approval respond <approval-id> --option <option-id>`.

Exploratory raw GitHub paths for `usnistgov/800-63-4/main/sp800-63b/authenticators.md`, `wenet-e2e/wespeaker/master/docs/onnx.md` and `wenet-e2e/wespeaker/master/runtime/README.md` returned 404; they are not evidence. The working WeSpeaker model catalog was used instead.

## Evidence classification

- **Observed repository facts:** current code paths, dependencies, VAD segmentation, event formatting, persistence and interruption behavior.
- **Published upstream claims:** supported APIs/platforms and benchmark numbers, with source-specific caveats.
- **Engineering proposals:** backend preference, enrollment duration, window/hop sizes, storage layout, new commands, routing states and rollout plan.
- **Unmeasured:** this owner’s recognition accuracy, target-Mac latency/memory, multilingual/device robustness, operational thresholds and resistance to replay/cloning.
